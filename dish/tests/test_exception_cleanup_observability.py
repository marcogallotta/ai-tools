"""Unexpected domain failures and secondary cleanup failures stay observable."""

from __future__ import annotations

import logging

import pytest

from dish_service.application import (
    DishService,
    ServiceConfig,
    _AdminExecutionState,
    _AgentExecutionState,
)
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.admin import DishAdminApplication
from dish_tool.database import confirm_task_content, create_operation
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.verification import make_app


def _leased_state(tmp_path, *, admin: bool):
    conn = initialize_database(tmp_path / ("admin.db" if admin else "agent.db"))
    identity = confirm_task_content(
        conn,
        task_gid="t",
        title="Title",
        notes="",
        schema_version="2",
        boundary="test",
    )
    operation = create_operation(
        conn,
        task_gid="t",
        operation_kind="planning",
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="rq",
    )
    principal = ServicePrincipal(
        owner_id="admin:marco" if admin else "agent:gpt", run_id="run"
    )
    leases = LeaseManager(conn, ttl_seconds=90)
    leases.acquire(operation["operation_id"], principal)
    return conn, operation["operation_id"], principal, leases


def test_unexpected_snapshot_validation_failure_is_not_reclassified(monkeypatch, tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)

    def fail_validation(*_args, **_kwargs):
        raise RuntimeError("validator programming defect")

    monkeypatch.setattr(
        "dish_tool.task_document.validate_task_document", fail_validation
    )
    with pytest.raises(RuntimeError, match="validator programming defect"):
        app.operation_service.current.authoritative_view(operation_id)


def test_agent_audit_repair_failure_is_logged_and_returned(monkeypatch, caplog, tmp_path):
    app, _backend, operation_id, _ = make_app(tmp_path)

    def fail_repair(_conn):
        raise RuntimeError("repair worker unavailable")

    monkeypatch.setattr("dish_tool.audit_repair.process_command_audit_repairs", fail_repair)
    with caplog.at_level(logging.ERROR, logger="dish_tool.audit_repair"):
        result = app.execute("inspect", agent="gpt", submission_id=operation_id)

    assert result["ok"]
    assert result["data"]["audit_repair_processing_warning"] == {
        "kind": "pending_invocation_audit_repair",
        "surface": "dish",
        "error_type": "RuntimeError",
        "current_command_committed": True,
    }
    assert "pending invocation-audit repair processing failed" in caplog.text


def test_admin_audit_repair_failure_is_returned(tmp_path, monkeypatch):
    app, backend, operation_id, _ = make_app(tmp_path)
    admin = DishAdminApplication(
        app.conn, backend=backend, release_loader=lambda: app._load_release(None)
    )
    monkeypatch.setattr(
        "dish_tool.audit_repair.process_command_audit_repairs",
        lambda _conn: (_ for _ in ()).throw(OSError("sidecar unavailable")),
    )

    result = admin.record_argument_failure(
        "recover",
        DishRuleError(
            "INVALID_ARGUMENT",
            "invalid recovery request",
            rule="invalid_recovery_request",
        ),
        submission_id=operation_id,
    )

    assert result["data"]["audit_repair_processing_warning"] == {
        "kind": "pending_invocation_audit_repair",
        "surface": "dish-admin",
        "error_type": "OSError",
        "current_command_committed": False,
    }


def test_agent_rejection_preserves_rule_and_exposes_failed_lease_cleanup(
    monkeypatch, tmp_path
):
    conn, operation_id, principal, leases = _leased_state(tmp_path, admin=False)
    service = DishService(
        ServiceConfig(db_path=tmp_path / "agent.db", honest_root=tmp_path),
        backend_factory=lambda: object(),
    )
    state = _AgentExecutionState(
        conn=conn,
        principal=principal,
        leases=leases,
        invocation_run_id=principal.run_id,
        prepared_arguments={},
        operation_id=operation_id,
        acquired_for_request=True,
    )
    monkeypatch.setattr(
        leases,
        "release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("release failed")
        ),
    )

    result = service._agent_rule_error_result(
        state,
        command="prepare",
        arguments={"submission_id": operation_id},
        request_id=None,
        error=DishRuleError("WRONG_STATE", "blocked", rule="operation_not_ready"),
    )

    assert result["errors"][0]["rule"] == "operation_not_ready"
    assert result["allowed_actions"] == []
    assert result["retryable"] is False
    warning = result["data"]["service_cleanup_warning"]
    assert warning["kind"] == "rejected_command_lease_release"
    assert warning["error_type"] == "RuntimeError"
    assert warning["lease_still_active"] is True
    assert result["data"]["service_recovery_required"] is True
    conn.close()


def test_admin_rejection_preserves_rule_and_exposes_failed_lease_cleanup(
    monkeypatch, tmp_path
):
    conn, operation_id, principal, leases = _leased_state(tmp_path, admin=True)
    service = DishService(
        ServiceConfig(db_path=tmp_path / "admin.db", honest_root=tmp_path),
        backend_factory=lambda: object(),
    )
    state = _AdminExecutionState(
        conn=conn,
        principal=principal,
        leases=leases,
        prepared_arguments={},
        operation_id=operation_id,
        supplied_run_id=principal.run_id,
        acquired_for_request=True,
    )
    monkeypatch.setattr(
        leases,
        "release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("release failed")
        ),
    )

    result = service._admin_rule_error_result(
        state,
        command="recover",
        request_id=None,
        error=DishRuleError("WRONG_STATE", "blocked", rule="operation_not_recoverable"),
    )

    assert result["errors"][0]["rule"] == "operation_not_recoverable"
    assert result["allowed_actions"] == []
    assert result["retryable"] is False
    assert result["data"]["service_cleanup_warning"]["error_type"] == "RuntimeError"
    assert result["data"]["service_recovery_required"] is True
    conn.close()


def test_admin_unexpected_handler_failure_keeps_safe_diagnostic(monkeypatch, caplog, tmp_path):
    import dish_tool.admin as admin_module

    conn = initialize_database(tmp_path / "admin-unexpected.db")
    admin = DishAdminApplication(conn)

    def crash(_app, *, trace):
        raise RuntimeError("secret payload must not escape")

    monkeypatch.setattr(admin, "validate_arguments", lambda _command, _arguments: crash)
    with caplog.at_level(logging.ERROR, logger="dish.admin"):
        result = admin.execute("attention")

    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0] == {
        "rule": "unexpected_internal_failure",
        "error_type": "RuntimeError",
    }
    assert "secret payload must not escape" not in str(result)
    assert "unexpected_admin_failure" in caplog.text
    assert "RuntimeError" in caplog.text
    conn.close()


def test_audit_repair_sink_failure_is_reported_not_silent(monkeypatch, caplog, tmp_path):
    import dish_tool.database as database
    from dish_tool.audit_repair import attempt_command_audit_repairs

    conn = initialize_database(tmp_path / "audit-repair.db")
    repair_id = database.record_command_audit_repair(
        conn,
        command="inspect",
        result={"ok": True, "code": "OK", "state": None, "retryable": False, "errors": []},
        audit_error="original audit failure",
    )
    conn.commit()
    monkeypatch.setattr(
        database,
        "record_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sink unavailable")),
    )

    with caplog.at_level(logging.ERROR, logger="dish_tool.audit_repair"):
        attempt = attempt_command_audit_repairs(conn, surface="test")

    assert attempt.repaired == 0
    assert attempt.error_type == "RuntimeError"
    assert "pending invocation-audit repair processing failed" in caplog.text
    row = conn.execute(
        "SELECT repaired_at FROM command_audit_repairs WHERE repair_id=?", (repair_id,)
    ).fetchone()
    assert row["repaired_at"] is None
    conn.close()


def test_cli_startup_runtime_error_keeps_safe_type_not_message(monkeypatch, caplog, capsys):
    from dish_service import cli

    monkeypatch.setattr(
        cli,
        "build_application",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("credential=do-not-print")),
    )
    with caplog.at_level(logging.ERROR, logger="dish.cli"):
        status = cli.main(["read", "123", "--agent", "gpt"])

    payload = __import__("json").loads(capsys.readouterr().out)
    assert status != 0
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["errors"][0] == {"rule": "startup_failure", "error_type": "RuntimeError"}
    assert "credential=do-not-print" not in str(payload)
    assert "dish_startup_failure" in caplog.text


def test_confirmed_resting_status_parse_failure_is_logged(monkeypatch, caplog, tmp_path):
    import dish_tool.admin as admin_module
    import dish_tool.task_document as task_document

    conn = initialize_database(tmp_path / "resting-status.db")
    confirm_task_content(
        conn,
        task_gid="123",
        title="Dish",
        notes="STATE: READY",
        schema_version="2",
        boundary="test",
    )
    monkeypatch.setattr(
        task_document,
        "parse_task_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad internal parse detail")),
    )

    with caplog.at_level(logging.ERROR, logger="dish.admin"):
        assert admin_module._confirmed_resting_status(conn, "123") is None

    assert "confirmed_resting_status_parse_failed" in caplog.text
    assert "ValueError" in caplog.text
    conn.close()


def test_consequential_broad_catches_preserve_observability_or_reraise():
    import ast
    import inspect
    import textwrap
    from dish_service import admin_cli, cli
    import dish_tool.admin as admin_module
    import dish_tool.database as database

    targets = (
        admin_module.DishAdminApplication.execute,
        admin_module._confirmed_resting_status,
        database.process_command_audit_repairs,
        cli.main,
        admin_cli.main,
    )

    silent = []
    for target in targets:
        tree = ast.parse(textwrap.dedent(inspect.getsource(target)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                continue
            has_reraise = any(isinstance(child, ast.Raise) for child in ast.walk(node))
            has_log = any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"error", "exception", "critical"}
                for child in ast.walk(node)
            )
            has_safe_type = any(
                isinstance(child, ast.Constant) and child.value == "error_type"
                for child in ast.walk(node)
            )
            if not (has_reraise or has_log or has_safe_type):
                silent.append(target.__qualname__)
    assert not silent, f"silent consequential broad catches: {silent}"
