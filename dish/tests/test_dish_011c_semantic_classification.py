from __future__ import annotations

import sqlite3

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r46_operational_hardening import _service


def _semantic_failure(record_id: str) -> DishRuleError:
    return DishRuleError(
        "VALIDATION_FAILED",
        "database durable evidence is semantically inconsistent",
        rule="database_semantic_evidence_invalid",
        retryable=False,
        details={
            "problems": [{
                "invariant": "content_identity_mismatch",
                "record_type": "content_versions",
                "record_id": record_id,
            }]
        },
    )


@pytest.mark.parametrize(
    ("surface", "command", "arguments", "request_id"),
    [
        (
            "agent",
            "create",
            {"agent": "gpt", "title": "Semantic classification probe"},
            "55555555-5555-4555-8555-555555555555",
        ),
        (
            "admin",
            "migrate",
            {"task_gid": "123456789"},
            "66666666-6666-4666-8666-666666666666",
        ),
    ],
)
def test_post_journal_service_paths_preserve_semantic_classification(
    tmp_path, monkeypatch, surface, command, arguments, request_id
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal(f"{surface}-owner", f"{surface}-run")
    calls = 0

    def fail_mutation_readiness(_backend):
        nonlocal calls
        calls += 1
        raise _semantic_failure(f"{surface}-post-journal")

    monkeypatch.setattr(service, "_assert_mutation_ready", fail_mutation_readiness)
    invoke = service.execute_agent if surface == "agent" else service.execute_admin

    first = invoke(command, arguments, principal=principal, request_id=request_id)
    replay = invoke(command, arguments, principal=principal, request_id=request_id)

    assert calls == 1
    assert first["code"] == "VALIDATION_FAILED"
    assert first["retryable"] is True
    error = first["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is True
    assert error["request_id_consumed"] is True
    assert error["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )
    assert first["data"]["request_id"] == request_id
    assert replay["data"]["request_replayed"] is True
    assert replay["errors"][0]["rule"] == "database_semantic_evidence_invalid"


@pytest.mark.parametrize("command", ["renew-lease", "recover-lease"])
def test_lease_service_paths_preserve_semantic_classification(
    tmp_path, monkeypatch, command
):
    service, _backend = _service(tmp_path)
    operation_id = "77777777-7777-4777-8777-777777777777"
    owner = ServicePrincipal("lease-owner", "lease-run")
    admin = ServicePrincipal("admin-owner", "admin-run")
    conn = initialize_database(service.config.db_path)
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            "task-lease-semantic",
            "initial",
            "open",
            "identity",
            "2",
            "2026-07-28T00:00:00Z",
            "prepare_required",
            "research",
        ),
    )
    LeaseManager(conn).acquire(operation_id, owner)
    conn.close()

    calls = 0

    def fail_lease_operation(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise _semantic_failure(f"{command}-post-journal")

    method = "renew" if command == "renew-lease" else "admin_recover"
    monkeypatch.setattr(LeaseManager, method, fail_lease_operation)
    request_id = (
        "88888888-8888-4888-8888-888888888888"
        if command == "renew-lease"
        else "99999999-9999-4999-8999-999999999999"
    )
    if command == "renew-lease":
        invoke = lambda: service.renew_lease(operation_id, owner, request_id=request_id)
    else:
        invoke = lambda: service.recover_lease(
            operation_id,
            admin,
            reason="semantic diagnostic probe",
            request_id=request_id,
        )

    first = invoke()
    replay = invoke()

    assert calls == 1
    assert first["code"] == "VALIDATION_FAILED"
    error = first["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is True
    assert error["request_id_consumed"] is True
    assert error["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )
    assert replay["data"]["request_replayed"] is True


def test_restore_path_preserves_semantic_classification_and_replay(
    tmp_path, monkeypatch
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal("admin-owner", "admin-run")
    request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    calls = 0

    class SemanticFailureRestoreManager:
        def set_restore_checkpoint(self, callback):
            self.callback = callback

        def restore(self, backup_id):
            nonlocal calls
            calls += 1
            assert backup_id == "semantic-backup"
            raise _semantic_failure("restore-post-journal")

    manager = SemanticFailureRestoreManager()
    monkeypatch.setattr(
        type(service),
        "backup_manager",
        property(lambda _self: manager),
    )

    first = service.restore_backup(
        "semantic-backup", principal=principal, request_id=request_id
    )
    replay = service.restore_backup(
        "semantic-backup", principal=principal, request_id=request_id
    )

    assert calls == 1
    assert first["code"] == "VALIDATION_FAILED"
    error = first["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is True
    assert error["request_id_consumed"] is True
    assert error["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )
    assert replay["data"]["request_replayed"] is True


def test_unclassified_database_wrapper_does_not_flatten_semantic_failure():
    from dish_service import application as service_application

    preserved = service_application._database_execution_unavailable_error(
        _semantic_failure("wrapped-semantic"), request_id_consumed=True
    )

    assert preserved.code == "VALIDATION_FAILED"
    assert preserved.rule == "database_semantic_evidence_invalid"
    assert preserved.retryable is True
    assert preserved.details["execution_occurred"] is True
    assert preserved.details["request_id_consumed"] is True
    assert preserved.details["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )


def test_nested_application_result_is_normalized_at_service_boundary():
    from dish_service import application as service_application
    from dish_tool.results import error_envelope

    result = error_envelope("create", _semantic_failure("nested-result"))
    normalized = service_application._preserve_semantic_evidence_result(
        result,
        execution_occurred=True,
        request_id_consumed=True,
    )

    assert normalized["code"] == "VALIDATION_FAILED"
    assert normalized["retryable"] is True
    error = normalized["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is True
    assert error["request_id_consumed"] is True
    assert error["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )
