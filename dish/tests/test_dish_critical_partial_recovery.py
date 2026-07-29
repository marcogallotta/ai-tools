from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.admin import DishAdminApplication
from dish_tool.database import initialize_database
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.step6 import prepare_live
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_r54_hard_request_identity import _service
from tests.test_dish_tool_step6_prepare import Backend, TASK, app, write


def _started_application(tmp_path):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="initial",
        change_level=None,
        change_reason=None,
    )
    return application, backend, started["submission_id"]


def _fault_at_step(monkeypatch, step_name):
    import dish_tool.step6 as step6

    real_complete = step6.complete_operation_step

    def fail_once(conn, operation_id, current_step):
        if current_step == step_name:
            raise RuntimeError(f"fault after {step_name}")
        return real_complete(conn, operation_id, current_step)

    monkeypatch.setattr(step6, "complete_operation_step", fail_once)


@pytest.mark.parametrize(
    ("fault_step", "write_committed", "cycle_created", "move_committed"),
    [
        ("candidate_write", True, False, False),
        ("handoff_validation", True, False, False),
        ("verification_cycle", True, True, False),
        ("verification_handoff", True, True, True),
    ],
)
def test_partial_failures_report_request_scoped_durable_evidence(
    tmp_path,
    monkeypatch,
    fault_step,
    write_committed,
    cycle_created,
    move_committed,
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate.md", TASK)

    with monkeypatch.context() as fault:
        _fault_at_step(fault, fault_step)
        result = application.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=operation_id,
            file_path=candidate,
        )

    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["retryable"] is False
    assert result["data"]["write_committed"] is write_committed
    assert result["data"]["cycle_created"] is cycle_created
    assert result["data"]["move_committed"] is move_committed
    assert result["data"]["failed_step"] == fault_step
    assert result["data"]["authoritative_task_identity"]
    assert result["data"]["authoritative_identity_source"] == (
        "execution_confirmed_content_version"
    )
    assert result["data"]["required_admin_action"] == "recover"
    assert result["data"]["safe_to_retry"] is False
    assert result["allowed_actions"] == []

    execution = application.conn.execute(
        "SELECT * FROM operation_executions WHERE execution_id=?",
        (result["data"]["execution_id"],),
    ).fetchone()
    assert execution["status"] == "uncertain"
    assert execution["request_id"] is None
    assert application.conn.execute(
        "SELECT COUNT(*) FROM operation_execution_claims"
    ).fetchone()[0] == 0

    writes_before = backend.writes
    moves_before = backend.moves
    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="fault-window reconciliation",
    )
    assert recovered["ok"]
    assert backend.writes == writes_before
    if fault_step == "verification_handoff":
        assert backend.moves == moves_before
    else:
        assert backend.moves <= moves_before + 1
    assert application.conn.execute(
        "SELECT COUNT(*) FROM verification_cycles WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 1


def test_pending_request_reconstructs_same_recovery_state_after_restart(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    start_request = "10000000-0000-4000-8000-000000000101"
    prepare_request = "10000000-0000-4000-8000-000000000102"
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id=start_request,
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    arguments = {
        "agent": "gpt",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "file_text": backend.title + "\n" + backend.notes,
    }
    prepared_arguments = service._arguments_for_principal(
        "prepare", arguments, run_id=principal.run_id
    )

    conn = initialize_database(service.config.db_path)
    try:
        _row, created = begin_request(
            conn,
            request_id=prepare_request,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command="prepare",
            arguments=prepared_arguments,
        )
        assert created
        claim = claim_operation_execution(
            conn,
            operation_id=operation_id,
            command="prepare",
            request_id=prepare_request,
        )
        candidate = tmp_path / "crash-candidate.md"
        candidate.write_text(backend.title + "\n" + backend.notes)
        import dish_tool.step6 as step6

        real_complete = step6.complete_operation_step

        def crash_after_move(connection, current_operation, step_name):
            if step_name == "verification_handoff":
                raise SystemExit("simulated process termination")
            return real_complete(connection, current_operation, step_name)

        with monkeypatch.context() as fault:
            fault.setattr(step6, "complete_operation_step", crash_after_move)
            with pytest.raises(SystemExit):
                prepare_live(
                    conn,
                    backend,
                    operation_id=operation_id,
                    agent="gpt",
                    model="gpt-5.6-sol",
                    file_path=str(candidate),
                    release=service._release("research"),
                )
        # Deliberately do not finish or release the execution claim. This is the
        # durable state a killed process leaves behind.
        assert claim.request_id == prepare_request
    finally:
        conn.close()

    writes = backend.writes
    moves = backend.moves
    backend_factory_calls = 0

    def unavailable_backend():
        nonlocal backend_factory_calls
        backend_factory_calls += 1
        raise RuntimeError("backend must not hide durable recovery evidence")

    restarted = DishService(
        service.config,
        backend_factory=unavailable_backend,
        release_loader=_release_loader(service.config.honest_root),
    )
    import dish_tool.operation_execution as operation_execution

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        recovered = restarted.execute_agent(
            "prepare",
            arguments,
            principal=principal,
            request_id=prepare_request,
        )
    assert recovered["code"] == "BACKEND_UNCERTAIN"
    assert recovered["data"]["write_committed"] is True
    assert recovered["data"]["cycle_created"] is True
    assert recovered["data"]["move_committed"] is True
    assert recovered["data"]["failed_step"] == "verification_handoff"
    assert recovered["data"]["required_admin_action"] == "recover"
    assert recovered["data"]["required_admin_outcome"] == "applied"
    assert recovered["data"]["safe_to_retry"] is False
    assert backend.writes == writes
    assert backend.moves == moves
    assert backend_factory_calls == 0

    exact = restarted.execute_agent(
        "prepare",
        arguments,
        principal=principal,
        request_id=prepare_request,
    )
    assert exact["code"] == "BACKEND_UNCERTAIN"
    assert exact["data"]["request_replayed"] is True
    assert exact["data"]["execution_id"] == recovered["data"]["execution_id"]
    assert backend.writes == writes
    assert backend.moves == moves
    assert backend_factory_calls == 0

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        admin_conn = initialize_database(service.config.db_path)
        try:
            admin = DishAdminApplication(
                admin_conn,
                backend=backend,
                release_loader=lambda: service._release(
                    None, include_migrations=True
                ),
            )
            applied = admin.execute(
                "recover",
                submission_id=operation_id,
                outcome="applied",
                reason="reconcile interrupted prepare",
            )
        finally:
            admin_conn.close()
    assert applied["ok"]
    assert backend.writes == writes
    assert backend.moves == moves


def test_recovery_execution_detects_updates_to_preexisting_attempts_and_steps(
    tmp_path, monkeypatch
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate.md", TASK)

    import dish_tool.operation_execution as operation_execution
    import dish_tool.task_store as task_store

    real_finalize = task_store.finalize_confirmed_write_attempt

    def terminate_after_external_write(*args, **kwargs):
        raise SystemExit("terminated before confirmed write persistence")

    with monkeypatch.context() as killed:
        killed.setattr(
            task_store, "finalize_confirmed_write_attempt", terminate_after_external_write
        )
        with pytest.raises(SystemExit):
            application.execute(
                "prepare",
                agent="gpt",
                model="gpt-5.6-sol",
                submission_id=operation_id,
                file_path=candidate,
            )

    assert backend.writes == 1
    attempt = application.conn.execute(
        "SELECT * FROM write_attempts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert attempt["outcome"] == "started"
    assert application.conn.execute(
        "SELECT completed_at FROM operation_steps "
        "WHERE operation_id=? AND step_name='candidate_write'",
        (operation_id,),
    ).fetchone()[0] is None

    import dish_tool.database as database

    def finalize_then_crash(*args, **kwargs):
        version = real_finalize(*args, **kwargs)
        from dish_tool.errors import DishRuleError

        raise DishRuleError(
            "CONFLICT",
            f"fault after binding {version['content_version_id']}",
            rule="post_write_recovery_conflict",
        )

    with monkeypatch.context() as recovery_fault:
        recovery_fault.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        recovery_fault.setattr(
            database, "finalize_confirmed_write_attempt", finalize_then_crash
        )
        admin = DishAdminApplication(
            application.conn,
            backend=backend,
            release_loader=lambda: application.release_loader(None),
        )
        failed = admin.execute(
            "recover",
            submission_id=operation_id,
            outcome="applied",
            reason="reconcile killed write",
        )

    assert failed["code"] == "BACKEND_UNCERTAIN"
    assert failed["data"]["command"] == "recover"
    assert failed["data"]["write_committed"] is True
    assert failed["data"]["write_state"] == "confirmed"
    assert failed["data"]["failed_step"] == "candidate_write"
    assert failed["data"]["authoritative_identity_source"] == (
        "execution_confirmed_content_version"
    )
    assert failed["data"]["required_admin_action"] == "recover"
    assert backend.writes == 1

    admin = DishAdminApplication(
        application.conn,
        backend=backend,
        release_loader=lambda: application.release_loader(None),
    )
    recovered = admin.execute(
        "recover",
        submission_id=operation_id,
        outcome="applied",
        reason="finish killed write recovery",
    )
    assert recovered["ok"]
    assert backend.writes == 1


def test_execution_baseline_does_not_attribute_prior_operation_history(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate.md", TASK)
    assert application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=candidate,
    )["ok"]

    from dish_tool.operation_execution import (
        execution_recovery_state,
        finish_operation_execution,
    )

    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="approve",
        request_id="20000000-0000-4000-8000-000000000201",
    )
    state = execution_recovery_state(
        application.conn,
        execution_id=claim.execution_id,
        failure_rule="fault_before_mutation",
    )
    assert state["effects_observed"] is False
    assert state["recovery_required"] is False
    assert state["write_committed"] is False
    assert state["move_committed"] is False
    assert state["cycle_created"] is False
    assert state["cycle_changed"] is False
    assert state["write_attempt_ids"] == []
    assert state["movement_attempt_ids"] == []
    finish_operation_execution(application.conn, claim, status="completed")


def test_live_exact_replay_does_not_finalize_an_active_execution(tmp_path):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="30000000-0000-4000-8000-000000000301",
    )
    operation_id = started["submission_id"]
    request_id = "30000000-0000-4000-8000-000000000302"
    arguments = {
        "agent": "gpt",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "file_text": backend.title + "\n" + backend.notes,
    }
    prepared = service._arguments_for_principal(
        "prepare", arguments, run_id=principal.run_id
    )
    conn = initialize_database(service.config.db_path)
    try:
        _row, created = begin_request(
            conn,
            request_id=request_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command="prepare",
            arguments=prepared,
        )
        assert created
        claim = claim_operation_execution(
            conn,
            operation_id=operation_id,
            command="prepare",
            request_id=request_id,
        )
    finally:
        conn.close()

    replay = service.execute_agent(
        "prepare", arguments, principal=principal, request_id=request_id
    )
    assert replay["code"] == "BACKEND_UNCERTAIN"
    assert replay["errors"][0]["rule"] == "service_request_pending"
    assert "write_committed" not in replay.get("data", {})
    assert backend.writes == 0
    assert backend.moves == 0

    conn = initialize_database(service.config.db_path)
    try:
        request = conn.execute(
            "SELECT status FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        execution = conn.execute(
            "SELECT status FROM operation_executions WHERE execution_id=?",
            (claim.execution_id,),
        ).fetchone()
        execution_claim = conn.execute(
            "SELECT 1 FROM operation_execution_claims WHERE execution_id=?",
            (claim.execution_id,),
        ).fetchone()
        assert request["status"] == "pending"
        assert execution["status"] == "started"
        assert execution_claim is not None
    finally:
        conn.close()


def test_dead_exact_execution_without_effects_resumes_same_request(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="35000000-0000-4000-8000-000000000351",
    )
    operation_id = started["submission_id"]
    request_id = "35000000-0000-4000-8000-000000000352"
    arguments = {
        "agent": "gpt",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "file_text": backend.title + "\n" + backend.notes,
    }
    prepared = service._arguments_for_principal(
        "prepare", arguments, run_id=principal.run_id
    )
    conn = initialize_database(service.config.db_path)
    try:
        _row, created = begin_request(
            conn,
            request_id=request_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command="prepare",
            arguments=prepared,
        )
        assert created
        claim = claim_operation_execution(
            conn,
            operation_id=operation_id,
            command="prepare",
            request_id=request_id,
        )
    finally:
        conn.close()

    import dish_tool.operation_execution as operation_execution

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        resumed = service.execute_agent(
            "prepare", arguments, principal=principal, request_id=request_id
        )
    assert resumed["ok"]
    assert backend.writes == 1
    assert backend.moves == 1

    conn = initialize_database(service.config.db_path)
    try:
        executions = conn.execute(
            "SELECT execution_id,status FROM operation_executions WHERE request_id=?",
            (request_id,),
        ).fetchall()
        assert [(row["execution_id"], row["status"]) for row in executions] == [
            (claim.execution_id, "completed")
        ]
        assert conn.execute(
            "SELECT 1 FROM operation_execution_claims WHERE execution_id=?",
            (claim.execution_id,),
        ).fetchone() is None
    finally:
        conn.close()


def test_dead_claim_with_local_state_change_requires_recover_without_pending_steps(
    tmp_path, monkeypatch
):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.database import transition_operation
    from dish_tool.errors import DishRuleError
    from dish_tool.operation_execution import finish_operation_execution
    import dish_tool.operation_execution as operation_execution

    claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="discard",
        request_id="40000000-0000-4000-8000-000000000401",
    )
    transition_operation(
        application.conn,
        operation_id,
        phase="terminal",
        status="cancelled",
        terminal_outcome="cancelled_by_marco",
    )
    assert application.conn.execute(
        "SELECT COUNT(*) FROM operation_steps WHERE operation_id=? AND completed_at IS NULL",
        (operation_id,),
    ).fetchone()[0] == 0

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        with pytest.raises(DishRuleError) as blocked:
            claim_operation_execution(
                application.conn,
                operation_id=operation_id,
                command="prepare",
                request_id="40000000-0000-4000-8000-000000000402",
            )
        assert blocked.value.rule == "operation_mutation_recovery_required"
        assert blocked.value.details["required_admin_action"] == "recover"

        recovery_claim = claim_operation_execution(
            application.conn,
            operation_id=operation_id,
            command="recover",
            request_id="40000000-0000-4000-8000-000000000403",
        )
    prior = application.conn.execute(
        "SELECT status,evidence_json FROM operation_executions "
        "WHERE request_id='40000000-0000-4000-8000-000000000401'"
    ).fetchone()
    assert prior["status"] == "uncertain"
    assert json.loads(prior["evidence_json"])["local_state_committed"] is True
    finish_operation_execution(application.conn, recovery_claim, status="completed")


def test_admin_pending_request_reconstructs_preconstruction_resolution_after_restart(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    import dish_tool.operation_execution as operation_execution
    import dish_tool.step8 as step8

    agent = ServicePrincipal(
        owner_id="action", run_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=agent,
        request_id="50000000-0000-4000-8000-000000000500",
    )
    assert started["ok"]
    operation_id = started["submission_id"]
    held = service.execute_agent(
        "reject",
        {
            "agent": "gpt",
            "submission_id": operation_id,
            "route": "evidence",
            "reason": "need authoritative input",
            "resume_status": "pending-research",
        },
        principal=agent,
        request_id="50000000-0000-4000-8000-000000000501",
    )
    assert held["ok"]

    principal = ServicePrincipal(
        owner_id="marco-admin", run_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    )
    request_id = "50000000-0000-4000-8000-000000000502"
    arguments = {
        "submission_id": operation_id,
        "detail": "Marco supplied the missing authority",
        "resume_status": "pending-research",
    }
    real_complete = step8.complete_operation_step

    def terminate_after_transition(conn, current_operation, step_name):
        if step_name == "research_preconstruction_hold_resolution":
            raise SystemExit("terminated before resolution completion")
        return real_complete(conn, current_operation, step_name)

    with monkeypatch.context() as killed:
        killed.setattr(step8, "complete_operation_step", terminate_after_transition)
        with pytest.raises(SystemExit):
            service.execute_admin(
                "supply-evidence",
                arguments,
                principal=principal,
                request_id=request_id,
            )

    conn = initialize_database(service.config.db_path)
    try:
        operation = conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert tuple(operation) == ("open", "prepare_required")
        pending = conn.execute(
            "SELECT completed_at FROM operation_steps "
            "WHERE operation_id=? AND step_name='research_preconstruction_hold_resolution'",
            (operation_id,),
        ).fetchone()
        assert pending["completed_at"] is None
    finally:
        conn.close()

    restarted = DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=_release_loader(service.config.honest_root),
    )
    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        recovered = restarted.execute_admin(
            "supply-evidence",
            arguments,
            principal=principal,
            request_id=request_id,
        )
    assert recovered["code"] == "BACKEND_UNCERTAIN"
    assert recovered["data"]["command"] == "supply-evidence"
    assert recovered["data"]["local_state_committed"] is True
    assert recovered["data"]["write_committed"] is False
    assert recovered["data"]["move_committed"] is False
    assert recovered["data"]["failed_step"] == (
        "research_preconstruction_hold_resolution"
    )
    assert recovered["data"]["required_admin_action"] == "recover"
    assert recovered["data"]["required_admin_outcome"] == "applied"
    assert recovered["data"]["safe_to_retry"] is False

    exact = restarted.execute_admin(
        "supply-evidence",
        arguments,
        principal=principal,
        request_id=request_id,
    )
    assert exact["data"]["request_replayed"] is True
    assert exact["data"]["execution_id"] == recovered["data"]["execution_id"]

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        reconciled = restarted.execute_admin(
            "recover",
            {
                "submission_id": operation_id,
                "outcome": "applied",
                "reason": "reconcile interrupted hold resolution",
            },
            principal=principal,
            request_id="50000000-0000-4000-8000-000000000503",
        )
    assert reconciled["ok"]
    assert any(
        action.get("step") == "research_preconstruction_hold_resolution"
        for action in reconciled["data"]["actions"]
    )



def test_controlled_error_after_committed_write_is_reported_as_partial(
    tmp_path, monkeypatch
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate-controlled.md", TASK)
    import dish_tool.step6 as step6
    from dish_tool.errors import DishRuleError

    def fail_after_write(*args, **kwargs):
        raise DishRuleError(
            "CONFLICT",
            "fault after confirmed write",
            rule="injected_controlled_failure",
        )

    with monkeypatch.context() as fault:
        fault.setattr(step6, "record_actor_fact", fail_after_write)
        result = application.execute(
            "prepare",
            agent="gpt",
            model="gpt-5.6-sol",
            submission_id=operation_id,
            file_path=candidate,
        )

    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "operation_partial_write_failure"
    assert result["data"]["write_committed"] is True
    assert result["data"]["failed_step"] == "handoff_validation"
    assert result["data"]["required_admin_action"] == "recover"
    assert result["data"]["safe_to_retry"] is False
    assert backend.writes == 1


def test_dead_no_effect_execution_retries_same_request_safely(tmp_path, monkeypatch):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="60000000-0000-4000-8000-000000000600",
    )
    operation_id = started["submission_id"]
    request_id = "60000000-0000-4000-8000-000000000601"
    arguments = {
        "agent": "gpt",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "file_text": backend.title + "\n" + backend.notes,
    }
    prepared = service._arguments_for_principal(
        "prepare", arguments, run_id=principal.run_id
    )
    conn = initialize_database(service.config.db_path)
    try:
        _row, created = begin_request(
            conn,
            request_id=request_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command="prepare",
            arguments=prepared,
        )
        assert created
        first = claim_operation_execution(
            conn,
            operation_id=operation_id,
            command="prepare",
            request_id=request_id,
        )
    finally:
        conn.close()

    import dish_tool.operation_execution as operation_execution

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        result = service.execute_agent(
            "prepare", arguments, principal=principal, request_id=request_id
        )
    assert result["ok"]
    assert backend.writes == 1

    conn = initialize_database(service.config.db_path)
    try:
        executions = conn.execute(
            "SELECT execution_id,status FROM operation_executions "
            "WHERE request_id=? ORDER BY created_at,rowid",
            (request_id,),
        ).fetchall()
        assert len(executions) == 1
        assert executions[0]["execution_id"] == first.execution_id
        assert executions[0]["status"] == "completed"
    finally:
        conn.close()


def test_unresolved_write_does_not_claim_baseline_identity_as_authoritative(
    tmp_path, monkeypatch
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate-unresolved.md", TASK)
    import dish_tool.task_store as task_store
    from dish_tool.operation_execution import execution_recovery_state

    def terminate_before_confirmation(*args, **kwargs):
        raise SystemExit("terminated before write confirmation was persisted")

    with monkeypatch.context() as killed:
        killed.setattr(
            task_store, "finalize_confirmed_write_attempt", terminate_before_confirmation
        )
        with pytest.raises(SystemExit):
            application.execute(
                "prepare",
                agent="gpt",
                model="gpt-5.6-sol",
                submission_id=operation_id,
                file_path=candidate,
            )

    execution = application.conn.execute(
        "SELECT execution_id FROM operation_executions "
        "WHERE operation_id=? AND status='started' ORDER BY created_at DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    state = execution_recovery_state(
        application.conn,
        execution_id=execution["execution_id"],
        failure_rule="process_terminated",
    )
    assert backend.writes == 1
    assert state["write_state"] == "uncertain"
    assert state["authoritative_task_identity"] is None
    assert state["authoritative_content_version_id"] is None
    assert state["authoritative_identity_source"] == "unresolved_external_write"
    assert state["required_admin_outcome"] == "inspect"


def test_completed_execution_recovery_does_not_absorb_later_operation_changes(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.operation_execution import (
        execution_recovery_state,
        finish_operation_execution,
    )

    completed = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="prepare",
        request_id="70000000-0000-4000-8000-000000000701",
    )
    finish_operation_execution(application.conn, completed, status="completed")
    application.conn.execute(
        "UPDATE operations SET phase='await_verification' WHERE operation_id=?",
        (operation_id,),
    )
    assert execution_recovery_state(
        application.conn, execution_id=completed.execution_id
    ) is None


def test_proven_not_applied_write_remains_backend_rejected(tmp_path):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "candidate-rejected.md", TASK)
    from dish_tool.errors import BackendFailure

    def reject_write(*, task_gid, title, notes):
        raise BackendFailure(
            "BACKEND_REJECTED", "temporary content rejection", retryable=True
        )

    backend.update_task_content = reject_write
    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=candidate,
    )

    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert backend.writes == 0
    attempt = application.conn.execute(
        "SELECT outcome FROM write_attempts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    assert attempt["outcome"] == "not_applied"
    execution = application.conn.execute(
        "SELECT status FROM operation_executions WHERE operation_id=? ORDER BY created_at DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert execution["status"] == "completed"


def test_dead_proven_not_applied_execution_reports_not_applied_recovery(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="ffffffff-ffff-4fff-8fff-ffffffffffff"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="80000000-0000-4000-8000-000000000801",
    )
    operation_id = started["submission_id"]
    request_id = "80000000-0000-4000-8000-000000000802"
    arguments = {
        "agent": "gpt",
        "model": "gpt-5.6-sol",
        "submission_id": operation_id,
        "file_text": backend.title + "\n" + backend.notes,
    }

    from dish_tool.errors import BackendFailure, DishRuleError
    import dish_tool.application_service as application_service
    import dish_tool.operation_execution as operation_execution

    def reject_write(*, task_gid, title, notes):
        raise BackendFailure(
            "BACKEND_REJECTED", "temporary content rejection", retryable=True
        )

    real_write = backend.update_task_content
    with monkeypatch.context() as killed:
        killed.setattr(backend, "update_task_content", reject_write)

        def terminate_before_journal_completion(*args, **kwargs):
            raise SystemExit("terminated before execution journal completion")

        killed.setattr(
            application_service,
            "finish_operation_execution",
            terminate_before_journal_completion,
        )
        with pytest.raises(SystemExit):
            service.execute_agent(
                "prepare", arguments, principal=principal, request_id=request_id
            )
    backend.update_task_content = real_write

    conn = initialize_database(service.config.db_path)
    try:
        with monkeypatch.context() as dead_process:
            dead_process.setattr(
                operation_execution,
                "process_identity_is_live",
                lambda _identity: False,
            )
            with pytest.raises(DishRuleError) as blocked:
                claim_operation_execution(
                    conn,
                    operation_id=operation_id,
                    command="submit",
                    request_id="80000000-0000-4000-8000-000000000803",
                )
        error = blocked.value
        assert getattr(error, "rule", None) == "operation_mutation_recovery_required"
        assert error.details["write_state"] == "not_applied"
        assert error.details["write_committed"] is False
        assert error.details["required_admin_action"] == "recover"
        assert error.details["required_admin_outcome"] == "not-applied"
        assert error.details["safe_to_retry"] is False
    finally:
        conn.close()

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution,
            "process_identity_is_live",
            lambda _identity: False,
        )
        replay = service.execute_agent(
            "prepare", arguments, principal=principal, request_id=request_id
        )
    assert replay["code"] == "BACKEND_UNCERTAIN"
    assert replay["data"]["write_state"] == "not_applied"
    assert replay["data"]["write_committed"] is False
    assert replay["data"]["required_admin_action"] == "recover"
    assert replay["data"]["required_admin_outcome"] == "not-applied"
    assert replay["data"]["safe_to_retry"] is False


def test_audit_only_local_effect_requires_recovery(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.database import record_audit
    from dish_tool.operation_execution import execution_recovery_state

    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="discard",
        request_id="90000000-0000-4000-8000-000000000901",
    )
    record_audit(
        application.conn,
        submission_id=None,
        task_gid="t",
        operation_id=operation_id,
        event_type="test.audit_only_effect",
        actor_agent=None,
        details={"reason": "fault-injection evidence"},
        result_code="OK",
        result_ok=True,
    )

    state = execution_recovery_state(
        application.conn,
        execution_id=claim.execution_id,
        failure_rule="process_terminated",
    )
    assert state["workflow_evidence_committed"] is True
    assert state["local_state_committed"] is True
    assert state["recovery_required"] is True
    assert state["required_admin_action"] == "recover"
    assert state["required_admin_outcome"] == "applied"
    assert state["safe_to_retry"] is False


def test_admin_recover_executes_immediately_under_exact_live_action_lease(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    action = ServicePrincipal(
        owner_id="action", run_id="abababab-abab-4bab-8bab-abababababab"
    )
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=action,
        request_id="71000000-0000-4000-8000-000000000001",
    )
    operation_id = started["submission_id"]

    import dish_tool.step6 as step6
    from dish_tool.errors import DishRuleError

    real_record_actor_fact = step6.record_actor_fact
    failed_once = False

    def fail_after_confirmed_write(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise DishRuleError(
                "CONFLICT",
                "fault after confirmed write",
                rule="injected_confirmed_write_failure",
            )
        return real_record_actor_fact(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(step6, "record_actor_fact", fail_after_confirmed_write)
        uncertain = service.execute_agent(
            "prepare",
            {
                "agent": "gpt",
                "model": "gpt-5.6-sol",
                "submission_id": operation_id,
                "file_text": backend.title + "\n" + backend.notes,
            },
            principal=action,
            request_id="71000000-0000-4000-8000-000000000002",
        )

    assert uncertain["code"] == "BACKEND_UNCERTAIN"
    assert uncertain["data"]["required_admin_action"] == "recover"
    assert uncertain["data"]["admin_recovery_lease_scope"] == (
        "exact_uncertain_execution"
    )
    assert uncertain["data"]["admin_recovery_immediately_executable"] is True
    assert backend.writes == 1

    conn = initialize_database(service.config.db_path)
    try:
        actor_lease = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        assert actor_lease["owner_id"] == action.owner_id
        assert actor_lease["run_id"] == action.run_id
        lease_id = actor_lease["lease_id"]
    finally:
        conn.close()

    admin = ServicePrincipal(
        owner_id="admin", run_id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
    )
    recovery_request = "71000000-0000-4000-8000-000000000003"
    recovered = service.execute_admin(
        "recover",
        {
            "submission_id": operation_id,
            "outcome": "applied",
            "reason": "reconcile confirmed Action write",
        },
        principal=admin,
        request_id=recovery_request,
    )
    assert recovered["ok"]
    assert backend.writes == 1

    replayed = service.execute_admin(
        "recover",
        {
            "submission_id": operation_id,
            "outcome": "applied",
            "reason": "reconcile confirmed Action write",
        },
        principal=admin,
        request_id=recovery_request,
    )
    assert replayed["ok"]
    assert replayed["data"]["request_replayed"] is True
    assert backend.writes == 1

    conn = initialize_database(service.config.db_path)
    try:
        surviving_lease = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
        assert surviving_lease["lease_id"] == lease_id
        assert surviving_lease["owner_id"] == action.owner_id
        assert surviving_lease["run_id"] == action.run_id
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE operation_id=? AND event_type='operation.recovery'",
            (operation_id,),
        ).fetchone()[0] == 1
        execution = conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (uncertain["data"]["execution_id"],),
        ).fetchone()
        assert execution["status"] == "completed"
        assert execution["resolution_evidence_json"] is not None
    finally:
        conn.close()
