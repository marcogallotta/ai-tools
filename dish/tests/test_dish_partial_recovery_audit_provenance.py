from __future__ import annotations

import json
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
from tests.support.thread_teardown import join_thread, managed_thread
from tests.support.verification import make_app




def test_real_prepare_binds_workflow_audits_but_not_invocation_audit(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "provenance-candidate.md", TASK)

    result = application.execute(
        "prepare",
        agent="gpt",
        model="gpt-5.6-sol",
        submission_id=operation_id,
        file_path=candidate,
    )
    assert result["ok"]
    execution = application.conn.execute(
        "SELECT execution_id FROM operation_executions "
        "WHERE operation_id=? AND command='prepare' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    workflow_events = application.conn.execute(
        "SELECT event_type FROM audit_events WHERE operation_execution_id=?",
        (execution["execution_id"],),
    ).fetchall()
    assert workflow_events
    invocation = application.conn.execute(
        "SELECT operation_execution_id FROM audit_events "
        "WHERE operation_id=? AND event_type='dish.prepare' "
        "ORDER BY rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert invocation["operation_execution_id"] is None


def test_audit_execution_binding_rejects_missing_operation(tmp_path):
    import sqlite3

    application, _backend, operation_id = _started_application(tmp_path)
    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="prepare",
        request_id="8f000000-0000-4000-8000-000000000901",
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit execution binding is invalid"):
        application.conn.execute(
            "INSERT INTO audit_events("
            "event_id,event_type,details,created_at,operation_execution_id"
            ") VALUES('bad-binding','test.bad','{}','now',?)",
            (claim.execution_id,),
        )


def test_execution_bound_audit_is_positive_recovery_evidence(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.database import record_audit
    from dish_tool.operation_execution import execution_recovery_state

    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="discard",
        request_id="90000000-0000-4000-8000-000000000901",
    )
    with operation_execution_provenance(
        application.conn,
        execution_id=claim.execution_id,
        operation_id=operation_id,
    ):
        record_audit(
            application.conn,
            submission_id=None,
            task_gid="t",
            operation_id=operation_id,
            event_type="operation.cancelled",
            actor_agent=None,
            details={"reason": "fault-injection evidence"},
            result_code="OK",
            result_ok=True,
        )

    audit = application.conn.execute(
        "SELECT operation_execution_id FROM audit_events "
        "WHERE operation_id=? AND event_type='operation.cancelled'",
        (operation_id,),
    ).fetchone()
    assert audit["operation_execution_id"] == claim.execution_id
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


def test_pre35_inflight_execution_retains_conservative_audit_fallback(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.database import record_audit
    from dish_tool.operation_execution import execution_recovery_state

    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="discard",
        request_id="90500000-0000-4000-8000-000000000901",
    )
    row = application.conn.execute(
        "SELECT baseline_json FROM operation_executions WHERE execution_id=?",
        (claim.execution_id,),
    ).fetchone()
    baseline = json.loads(row["baseline_json"])
    baseline.pop("audit_provenance_version")
    trigger_sql = application.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='operation_executions_identity_immutable_update'"
    ).fetchone()["sql"]
    application.conn.execute(
        "DROP TRIGGER operation_executions_identity_immutable_update"
    )
    try:
        application.conn.execute(
            "UPDATE operation_executions SET baseline_json=? WHERE execution_id=?",
            (
                json.dumps(baseline, sort_keys=True, separators=(",", ":")),
                claim.execution_id,
            ),
        )
    finally:
        application.conn.execute(trigger_sql)
    record_audit(
        application.conn,
        submission_id=None,
        task_gid="t",
        operation_id=operation_id,
        event_type="operation.cancelled",
        actor_agent=None,
        details={"reason": "legacy in-flight evidence"},
        result_code="OK",
        result_ok=True,
    )

    state = execution_recovery_state(
        application.conn,
        execution_id=claim.execution_id,
        failure_rule="process_terminated",
    )
    assert state["workflow_evidence_committed"] is True
    assert state["recovery_required"] is True
    assert state["safe_to_retry"] is False


@pytest.mark.flake_stress
def test_real_authorization_racing_no_effect_prepare_is_not_misattributed(
    tmp_path, monkeypatch
):
    application, backend, operation_id = _started_application(tmp_path)
    candidate = write(tmp_path, "authorization-race-candidate.md", TASK)
    database_path = application.conn.execute("PRAGMA database_list").fetchone()[2]
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    import dish_tool.step6 as step6

    def fail_after_authorization(*args, **kwargs):
        entered.set()
        assert release.wait(5), "authorization race did not complete"
        raise RuntimeError("injected no-effect prepare failure")

    monkeypatch.setattr(step6, "prepare_live", fail_after_authorization)

    def execute_prepare() -> None:
        conn = initialize_database(database_path)
        try:
            worker = DishApplication(
                conn,
                backend,
                release_loader=application.release_loader,
                invocation_run_id="constructor-race",
            )
            outcome["result"] = worker.execute(
                "prepare",
                agent="gpt",
                model="gpt-5.6-sol",
                submission_id=operation_id,
                file_path=str(candidate),
            )
        finally:
            conn.close()

    thread = managed_thread(target=execute_prepare)
    thread.start()
    assert entered.wait(5), "prepare execution did not reach the fault barrier"
    other = initialize_database(database_path)
    try:
        admin = DishAdminApplication(
            other, backend=backend, release_loader=lambda: application.release_loader(None)
        )
        granted = admin.execute(
            "authorize-governed-change",
            submission_id=operation_id,
            field="Purpose",
            before="Compare texture",
            after="Compare texture exactly",
            reason="Marco approved this exact Purpose change",
            run_id="marco-run",
        )
        assert granted["ok"]
    finally:
        other.close()
        release.set()
    join_thread(thread, timeout=5)
    assert not thread.is_alive()

    result = outcome["result"]
    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0]["rule"] == "unexpected_internal_failure"
    audit = application.conn.execute(
        "SELECT operation_execution_id FROM audit_events "
        "WHERE operation_id=? AND event_type='marco.authorization'",
        (operation_id,),
    ).fetchone()
    assert audit["operation_execution_id"] is None
    execution = application.conn.execute(
        "SELECT status,evidence_json FROM operation_executions "
        "WHERE operation_id=? AND command='prepare' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert execution["status"] == "completed"
    evidence = json.loads(execution["evidence_json"])
    assert evidence["effects_observed"] is False
    assert evidence["workflow_evidence_committed"] is False
    assert evidence["recovery_required"] is False
    assert evidence["safe_to_retry"] is True


@pytest.mark.flake_stress
def test_real_verifier_inspect_racing_no_effect_approve_is_not_misattributed(
    tmp_path, monkeypatch
):
    from dish_tool.application_service import CurrentWorkflowService

    application, backend, operation_id, _protocol = make_app(tmp_path)
    review = application.execute(
        "start",
        agent="codex",
        task_gid="t",
        kind="verification",
        run_id="inspect-race",
        independence_attestation="independent",
    )
    assert review["ok"]
    database_path = application.conn.execute("PRAGMA database_list").fetchone()[2]
    entered = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}
    real_assert_action = CurrentWorkflowService.assert_action

    def fail_after_inspect(self, current_operation_id, action, *, schema=None):
        if current_operation_id == operation_id and action == "approve":
            entered.set()
            assert release.wait(5), "inspect race did not complete"
            raise RuntimeError("injected no-effect approval failure")
        return real_assert_action(self, current_operation_id, action, schema=schema)

    monkeypatch.setattr(CurrentWorkflowService, "assert_action", fail_after_inspect)

    def execute_approve() -> None:
        conn = initialize_database(database_path)
        try:
            worker = DishApplication(
                conn,
                backend,
                release_loader=application.release_loader,
                invocation_run_id="inspect-race",
            )
            outcome["result"] = worker.execute(
                "approve",
                agent="codex",
                model="gpt-5.6-sol",
                submission_id=operation_id,
                correction="none",
                reviewed_identity=review["data"]["reviewed_identity"],
                semantic_review_complete=True,
                provenance_complete=True,
                run_id="inspect-race",
            )
        finally:
            conn.close()

    thread = managed_thread(target=execute_approve)
    thread.start()
    assert entered.wait(5), "approve execution did not reach the fault barrier"
    inspected = application.execute(
        "inspect", agent="codex", submission_id=operation_id
    )
    assert inspected["ok"]
    assert inspected["data"]["dish_inspect_fact"] is not None
    release.set()
    join_thread(thread, timeout=5)
    assert not thread.is_alive()

    result = outcome["result"]
    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0]["rule"] == "unexpected_internal_failure"
    audit = application.conn.execute(
        "SELECT operation_execution_id FROM audit_events "
        "WHERE operation_id=? AND event_type='verification.inspected'",
        (operation_id,),
    ).fetchone()
    assert audit["operation_execution_id"] is None
    assert application.conn.execute(
        "SELECT COUNT(*) FROM dish_inspect_facts WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] == 1
    execution = application.conn.execute(
        "SELECT status,evidence_json FROM operation_executions "
        "WHERE operation_id=? AND command='approve' "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (operation_id,),
    ).fetchone()
    assert execution["status"] == "completed"
    evidence = json.loads(execution["evidence_json"])
    assert evidence["effects_observed"] is False
    assert evidence["workflow_evidence_committed"] is False
    assert evidence["recovery_required"] is False
    assert evidence["safe_to_retry"] is True


def test_unrelated_invocation_audit_is_not_request_execution_evidence(tmp_path):
    application, _backend, operation_id = _started_application(tmp_path)
    from dish_tool.invocation_audit import record_invocation_audit
    from dish_tool.operation_execution import execution_recovery_state

    claim = claim_operation_execution(
        application.conn,
        operation_id=operation_id,
        command="reject",
        request_id="93000000-0000-4000-8000-000000000901",
    )
    record_invocation_audit(
        application.conn,
        surface="dish",
        command="inspect",
        result={
            "ok": True,
            "code": "OK",
            "state": "open",
            "retryable": False,
            "errors": [],
            "data": {"operation_id": operation_id},
        },
        task_gid="t",
        submission_id=operation_id,
        actor="gpt",
        actor_run_id="91919191-9191-4191-8191-919191919191",
    )

    state = execution_recovery_state(
        application.conn,
        execution_id=claim.execution_id,
        failure_rule="injected_no_effect_failure",
    )
    audit = application.conn.execute(
        "SELECT operation_execution_id FROM audit_events "
        "WHERE operation_id=? AND event_type='dish.inspect'",
        (operation_id,),
    ).fetchone()
    assert audit["operation_execution_id"] is None
    assert state["effects_observed"] is False
    assert state["workflow_evidence_committed"] is False
    assert state["committed_effects"] is False
    assert state["recovery_required"] is False
    assert state["required_admin_action"] is None
    assert state["required_admin_outcome"] is None
    assert state["admin_recovery_lease_scope"] is None
    assert state["admin_recovery_immediately_executable"] is False
    assert state["safe_to_retry"] is True


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

