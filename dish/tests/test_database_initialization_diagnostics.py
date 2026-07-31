from __future__ import annotations

import errno
import logging
import sqlite3
import traceback

import pytest

from dish_service import application as service_application
from dish_service.leases import ServicePrincipal
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.operational import _service


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("exc", "classification", "expected"),
    [
        (
            DishRuleError(
                "VALIDATION_FAILED",
                "database contract failed",
                rule="database_contract_failed",
                retryable=False,
            ),
            "dish_rule_error",
            {
                "original_code": "VALIDATION_FAILED",
                "original_rule": "database_contract_failed",
                "original_retryable": False,
            },
        ),
        (
            sqlite3.OperationalError("unable to open database file"),
            "sqlite_error",
            {},
        ),
        (
            OSError(errno.EACCES, "permission denied"),
            "filesystem_error",
            {"errno": errno.EACCES},
        ),
        (ValueError("invalid schema"), "database_contract_error", {}),
        (RuntimeError("unexpected"), "unexpected_error", {}),
    ],
)
def test_database_initialization_exception_classification(exc, classification, expected):
    returned, details = service_application._classify_database_initialization_exception(
        exc
    )

    assert returned == classification
    assert details["error_classification"] == classification
    assert details["error_type"] == type(exc).__name__
    for key, value in expected.items():
        assert details[key] == value


@pytest.mark.smoke
def test_database_initialization_failure_logs_original_exception_and_safe_request_context(
    tmp_path, monkeypatch, caplog
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal("agent-owner", "agent-run")
    request_id = "11111111-1111-4111-8111-111111111111"
    original = sqlite3.OperationalError("disk I/O error: original diagnostic")

    def fail_initialization(_db_path):
        raise original

    monkeypatch.setattr(service_application, "initialize_database", fail_initialization)
    arguments = {
        "agent": "gpt",
        "task_gid": "123456789",
        "kind": "initial",
        "file_text": "SENSITIVE CANDIDATE TEXT",
        "independence_attestation": "SENSITIVE ATTESTATION",
    }

    with caplog.at_level(logging.ERROR, logger="dish.service.application"):
        result = service.execute_agent(
            "start",
            arguments,
            principal=principal,
            request_id=request_id,
        )

    assert result["code"] == "INTERNAL_ERROR"
    assert result["retryable"] is True
    error = result["errors"][0]
    assert error["rule"] == "service_database_unavailable"
    assert error["error_type"] == "OperationalError"
    assert error["error_classification"] == "sqlite_error"
    assert error["execution_occurred"] is False
    assert error["request_id_consumed"] is False
    assert error["retry_condition"] == "after_database_availability_restored"

    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("database_initialization_failed")
    ]
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert "classification=sqlite_error" in message
    assert '"surface":"agent"' in message
    assert '"command":"start"' in message
    assert f'"request_id":"{request_id}"' in message
    assert '"owner_id":"agent-owner"' in message
    assert '"run_id":"agent-run"' in message
    assert '"task_gid":"123456789"' in message
    assert "SENSITIVE CANDIDATE TEXT" not in message
    assert "SENSITIVE ATTESTATION" not in message

    assert record.exc_info is not None
    assert record.exc_info[1] is original
    rendered_traceback = "".join(traceback.format_exception(*record.exc_info))
    assert "fail_initialization" in rendered_traceback
    assert "disk I/O error: original diagnostic" in rendered_traceback


@pytest.mark.smoke
def test_database_initialization_failure_logs_backup_request_context(
    tmp_path, monkeypatch, caplog
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal("admin-owner", "admin-run")
    request_id = "22222222-2222-4222-8222-222222222222"

    def fail_initialization(_db_path):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(service_application, "initialize_database", fail_initialization)

    with caplog.at_level(logging.ERROR, logger="dish.service.application"):
        result = service.create_backup(
            label="SENSITIVE BACKUP LABEL",
            principal=principal,
            request_id=request_id,
        )

    assert result["code"] == "INTERNAL_ERROR"
    error = result["errors"][0]
    assert error["error_classification"] == "filesystem_error"
    assert error["errno"] == errno.EACCES

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("database_initialization_failed")
    )
    assert '"surface":"admin"' in message
    assert '"command":"backup-create"' in message
    assert f'"request_id":"{request_id}"' in message
    assert '"owner_id":"admin-owner"' in message
    assert '"run_id":"admin-run"' in message
    assert "SENSITIVE BACKUP LABEL" not in message


@pytest.mark.smoke
def test_semantic_initialization_failure_keeps_classification_and_preexecution_retry(
    tmp_path,
):
    service, _backend = _service(tmp_path)
    conn = initialize_database(service.config.db_path)
    conn.execute(
        """INSERT INTO content_versions(
               content_version_id,task_gid,operation_id,boundary,identity,
               title,notes,confirmed,created_at
           ) VALUES(?,?,NULL,?,?,?,?,1,?)""",
        (
            "content-version-semantic-failure",
            "task-semantic-failure",
            "candidate",
            "SENSITIVE INVALID IDENTITY",
            "SENSITIVE CANDIDATE TITLE",
            "SENSITIVE CANDIDATE NOTES",
            "2026-07-28T00:00:00Z",
        ),
    )
    conn.close()

    backend_factory_called = False

    def fail_if_backend_created():
        nonlocal backend_factory_called
        backend_factory_called = True
        raise AssertionError("backend must not be created after initialization failure")

    service.backend_factory = fail_if_backend_created
    request_id = "33333333-3333-4333-8333-333333333333"
    result = service.execute_agent(
        "start",
        {
            "agent": "gpt",
            "task_gid": "task-semantic-failure",
            "kind": "initial",
            "file_text": "SENSITIVE REQUEST PAYLOAD",
        },
        principal=ServicePrincipal("agent-owner", "agent-run"),
        request_id=request_id,
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is True
    assert result["data"]["message"] == (
        "database durable evidence is semantically inconsistent"
    )
    error = result["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is False
    assert error["request_id_consumed"] is False
    assert error["retry_condition"] == "after_database_semantic_evidence_repair"
    problem = next(
        problem
        for problem in error["problems"]
        if problem["invariant"] == "content_identity_mismatch"
    )
    assert problem["record_type"] == "content_versions"
    assert problem["record_id"] == "content-version-semantic-failure"
    assert problem["mutation_provenance"] == {
        "task_gid": "task-semantic-failure",
    }
    assert problem["timestamps"] == {"created_at": "2026-07-28T00:00:00Z"}
    assert problem["broken_relationship"]["required_predicate"] == (
        "content_digest(title, notes) == identity"
    )
    assert error["transaction_state"] == {
        "connection_in_transaction": False,
        "evidence_visibility": "committed_database",
    }
    assert error["diagnostic_timestamp"].endswith("Z")
    assert backend_factory_called is False

    raw = sqlite3.connect(service.config.db_path)
    try:
        assert raw.execute(
            "SELECT COUNT(*) FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()[0] == 0
    finally:
        raw.close()

    rendered = repr(result)
    assert "service_database_unavailable" not in rendered
    assert "SENSITIVE INVALID IDENTITY" not in rendered
    assert "SENSITIVE CANDIDATE TITLE" not in rendered
    assert "SENSITIVE CANDIDATE NOTES" not in rendered
    assert "SENSITIVE REQUEST PAYLOAD" not in rendered


@pytest.mark.smoke
def test_semantic_failure_after_request_start_requires_fresh_request_id(
    tmp_path, monkeypatch
):
    service, _backend = _service(tmp_path)
    principal = ServicePrincipal("admin-owner", "admin-run")
    request_id = "44444444-4444-4444-8444-444444444444"
    calls = 0

    class SemanticFailureBackupManager:
        @staticmethod
        def new_backup_id(*, label):
            assert label == "semantic-check"
            return "dish-semantic-check.sqlite3"

        def create(self, *, label, backup_id=None):
            nonlocal calls
            calls += 1
            assert label == "semantic-check"
            assert backup_id == "dish-semantic-check.sqlite3"
            raise DishRuleError(
                "VALIDATION_FAILED",
                "database durable evidence is semantically inconsistent",
                rule="database_semantic_evidence_invalid",
                retryable=False,
                details={
                    "problems": [
                        {
                            "invariant": "content_identity_mismatch",
                            "record_type": "content_versions",
                            "record_id": "content-version-post-start",
                        }
                    ]
                },
            )

    manager = SemanticFailureBackupManager()
    monkeypatch.setattr(
        type(service),
        "backup_manager",
        property(lambda _self: manager),
    )

    result = service.create_backup(
        label="semantic-check",
        principal=principal,
        request_id=request_id,
    )

    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is True
    error = result["errors"][0]
    assert error["rule"] == "database_semantic_evidence_invalid"
    assert error["execution_occurred"] is True
    assert error["request_id_consumed"] is True
    assert error["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )
    assert error["problems"] == [
        {
            "invariant": "content_identity_mismatch",
            "record_type": "content_versions",
            "record_id": "content-version-post-start",
        }
    ]
    assert result["data"]["request_id"] == request_id

    replay = service.create_backup(
        label="semantic-check",
        principal=principal,
        request_id=request_id,
    )
    assert calls == 1
    assert replay["code"] == "VALIDATION_FAILED"
    assert replay["data"]["request_replayed"] is True
    assert replay["errors"][0]["retry_condition"] == (
        "after_database_semantic_evidence_repair_with_fresh_request_id"
    )

    raw = sqlite3.connect(service.config.db_path)
    try:
        row = raw.execute(
            "SELECT status, result_json FROM service_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "completed"
        assert row[1] is not None
    finally:
        raw.close()
