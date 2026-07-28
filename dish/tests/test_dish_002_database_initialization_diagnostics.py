from __future__ import annotations

import errno
import logging
import sqlite3
import traceback

import pytest

from dish_service import application as service_application
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r46_operational_hardening import _service


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
