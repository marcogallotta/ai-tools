from __future__ import annotations

import json
import shlex
import threading
from pathlib import Path

import pytest


from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.execution_provenance import operation_execution_provenance
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.step6 import prepare_live
from tests.support.partial_recovery import (
    Backend,
    TASK,
    app,
    release_loader as _release_loader,
    service as _service,
    write,
    started_application as _started_application,
    fault_at_step as _fault_at_step,
)


def _leave_interrupted_prepare_execution(
    service, backend, tmp_path, principal, prepare_request, monkeypatch
):
    started = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "initial"},
        principal=principal,
        request_id="10000000-0000-4000-8000-000000000101",
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
        assert claim.request_id == prepare_request
    finally:
        conn.close()
    return operation_id, arguments


def _reconstruct_prepare_recovery(
    service, arguments, principal, prepare_request, monkeypatch
):
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
            "prepare", arguments, principal=principal, request_id=prepare_request
        )
    exact = restarted.execute_agent(
        "prepare", arguments, principal=principal, request_id=prepare_request
    )
    return restarted, recovered, exact, backend_factory_calls


def _assert_reconstructed_prepare_recovery(recovered, exact):
    assert recovered["code"] == "BACKEND_UNCERTAIN"
    assert recovered["data"]["write_committed"] is True
    assert recovered["data"]["cycle_created"] is True
    assert recovered["data"]["move_committed"] is True
    assert recovered["data"]["failed_step"] == "verification_handoff"
    assert recovered["data"]["required_admin_action"] == "recover"
    assert recovered["data"]["required_admin_outcome"] == "applied"
    assert recovered["data"]["safe_to_retry"] is False
    assert exact["code"] == "BACKEND_UNCERTAIN"
    assert exact["data"]["request_replayed"] is True
    assert exact["data"]["execution_id"] == recovered["data"]["execution_id"]


def _admin_finish_interrupted_prepare(service, backend, operation_id, monkeypatch):
    import dish_tool.operation_execution as operation_execution

    with monkeypatch.context() as dead_process:
        dead_process.setattr(
            operation_execution, "process_identity_is_live", lambda _identity: False
        )
        admin_conn = initialize_database(service.config.db_path)
        try:
            admin = DishAdminApplication(
                admin_conn,
                backend=backend,
                release_loader=lambda: service._release(None, include_migrations=True),
            )
            return admin.execute(
                "recover",
                submission_id=operation_id,
                outcome="applied",
                reason="reconcile interrupted prepare",
            )
        finally:
            admin_conn.close()


def test_pending_request_reconstructs_same_recovery_state_after_restart(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    principal = ServicePrincipal(
        owner_id="action", run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    prepare_request = "10000000-0000-4000-8000-000000000102"
    operation_id, arguments = _leave_interrupted_prepare_execution(
        service, backend, tmp_path, principal, prepare_request, monkeypatch
    )
    writes, moves = backend.writes, backend.moves
    _restarted, recovered, exact, backend_calls = _reconstruct_prepare_recovery(
        service, arguments, principal, prepare_request, monkeypatch
    )
    _assert_reconstructed_prepare_recovery(recovered, exact)
    assert (backend.writes, backend.moves, backend_calls) == (writes, moves, 0)
    applied = _admin_finish_interrupted_prepare(
        service, backend, operation_id, monkeypatch
    )
    assert applied["ok"]
    assert (backend.writes, backend.moves) == (writes, moves)


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
        assert shlex.split(blocked.value.details["admin_command"]) == [
            "dish-admin",
            "recover",
            operation_id,
            "--outcome",
            "<inspect|not-applied|applied>",
            "--reason",
            "<summarize what the live reread showed>",
        ]
        assert "Tell Marco" in blocked.value.details["directive"]
        assert blocked.value.details["admin_command"] in blocked.value.details["directive"]

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

