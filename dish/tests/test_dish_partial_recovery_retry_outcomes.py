from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BIN))

from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request
from dish_tool.admin import DishAdminApplication
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.execution_provenance import operation_execution_provenance
from dish_tool.operation_execution import claim_operation_execution
from dish_tool.step6 import prepare_live
from tests._partial_recovery_helpers import (
    Backend,
    TASK,
    app,
    release_loader as _release_loader,
    service as _service,
    write,
    started_application as _started_application,
    fault_at_step as _fault_at_step,
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

