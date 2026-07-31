from __future__ import annotations

import json
import threading
import uuid
from http.client import HTTPConnection
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.backup import BackupManager
from dish_service.config import ServiceConfig
from dish_service.http import build_server
from dish_service.leases import ServicePrincipal
from tests._service_test_helpers import (
    OPERATION_ID,
    REQUEST_ID,
    RUN_ID,
    post as _post,
    running as _running,
)
from tests.support.service_foundation import _release_loader
from tests.support.request_restore import Backend



AGENT_ARGUMENTS = {
    "create": {"agent": "gpt", "title": "Dish"},
    "start": {"agent": "gpt", "task_gid": "123456789", "kind": "initial"},
    "prepare": {
        "agent": "gpt", "model": "model", "submission_id": OPERATION_ID,
        "file_text": "candidate",
    },
    "approve": {
        "agent": "gpt", "model": "model", "submission_id": OPERATION_ID,
        "correction": "none", "reviewed_identity": "identity",
        "semantic_review_complete": True, "provenance_complete": True,
    },
    "reject": {
        "agent": "gpt", "submission_id": OPERATION_ID,
        "reason": "blocked", "route": "evidence",
    },
    "submit": {"submission_id": OPERATION_ID},
}


@pytest.mark.parametrize("command", sorted(AGENT_ARGUMENTS))
def test_every_agent_mutation_requires_request_id(tmp_path, command):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            f"/v1/action/{command}",
            token="action-secret",
            payload={
                "client": {"run_id": RUN_ID},
                "arguments": AGENT_ARGUMENTS[command],
            },
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 200
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/admin/migrate", {"arguments": {}}),
        ("/v1/admin/recover", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/repair-destination", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/discard", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/reopen", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/supply-evidence", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/record-human-decision", {"arguments": {"submission_id": OPERATION_ID}}),
        ("/v1/admin/authorize-governed-change", {"arguments": {"submission_id": OPERATION_ID}}),
        (f"/v1/admin/leases/{OPERATION_ID}/recover", {"reason": "operator recovery"}),
        ("/v1/admin/backups/create", {"label": "manual"}),
        ("/v1/admin/backups/restore", {"backup_id": "missing.sqlite3"}),
    ],
)
def test_every_admin_and_service_state_mutation_requires_request_id(tmp_path, path, payload):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    payload = {**payload, "client": {"run_id": RUN_ID}}
    try:
        status, result = _post(url, path, token="admin-secret", payload=payload)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 400
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


def test_renew_lease_requires_request_id(tmp_path):
    _service_obj, _backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            f"/v1/leases/{OPERATION_ID}/renew",
            token="agent-secret",
            payload={"client": {"run_id": RUN_ID}},
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 400
    assert result["errors"] == [
        {"field": "client.request_id", "rule": "request_field_required"}
    ]


def test_malformed_request_id_is_not_recorded_and_identifies_field(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    try:
        _status, result = _post(
            url,
            "/v1/action/create",
            token="action-secret",
            payload={
                "client": {"run_id": RUN_ID, "request_id": "not-a-uuid"},
                "arguments": AGENT_ARGUMENTS["create"],
            },
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert result["errors"] == [
        {
            "field": "client.request_id",
            "rule": "uuid_identifier_required",
            "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        }
    ]
    from dish_tool.database import initialize_database
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "client"),
    [
        (
            "client.run_id",
            {"run_id": "00000000-0000-0000-0000-000000000000", "request_id": REQUEST_ID},
        ),
        (
            "client.request_id",
            {"run_id": RUN_ID, "request_id": "00000000-0000-0000-0000-000000000000"},
        ),
    ],
)
def test_nil_client_identities_are_rejected_before_request_journaling(
    tmp_path, field, client
):
    service, backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _post(
            url,
            "/v1/action/create",
            token="action-secret",
            payload={
                "client": client,
                "arguments": AGENT_ARGUMENTS["create"],
            },
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert status == 200
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {
            "field": field,
            "rule": "uuid_identifier_required",
            "expected_format": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        }
    ]
    assert backend.writes == 0

    from dish_tool.database import initialize_database

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_validation_failure_is_replayed_and_changed_reuse_conflicts(tmp_path):
    _service_obj, backend, server, thread, url = _running(tmp_path)
    payload = {
        "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
        "arguments": {"agent": "gpt", "task_gid": "bad-gid", "kind": "initial"},
    }
    try:
        first_status, first = _post(url, "/v1/action/start", token="action-secret", payload=payload)
        second_status, second = _post(url, "/v1/action/start", token="action-secret", payload=payload)
        changed = {
            **payload,
            "arguments": {"agent": "gpt", "task_gid": "different", "kind": "initial"},
        }
        conflict_status, conflict = _post(
            url, "/v1/action/start", token="action-secret", payload=changed
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert first_status == second_status == conflict_status == 200
    assert first["code"] == second["code"] == "INVALID_ARGUMENT"
    assert second["data"]["request_replayed"] is True
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"
    assert backend.writes == 0


