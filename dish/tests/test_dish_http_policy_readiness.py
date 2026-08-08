from __future__ import annotations

import json
import sqlite3
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dish_service import application as application_module
from dish_tool import database_schema as database_schema_module
from dish_tool.database_initialization import initialize_database
from tests.support.service_scenarios import (
    REQUEST_ID,
    RUN_ID,
    running as _running,
)
from tests.support.thread_teardown import join_thread, stop_server

OTHER_RUN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _raw_post(
    url: str,
    path: str,
    *,
    token: str,
    body: str,
    content_type: str | None,
) -> tuple[int, dict]:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Length": str(len(body.encode("utf-8"))),
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("path", "token", "payload", "expected_status"),
    [
        (
            "/v1/commands/sections",
            "agent-secret",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
            415,
        ),
        (
            "/v1/action/sections",
            "action-secret",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
            200,
        ),
        (
            "/v1/admin/backups/create",
            "admin-secret",
            {
                "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
                "label": "manual",
            },
            415,
        ),
    ],
)
def test_protected_json_routes_reject_non_json_media_type(
    tmp_path, path, token, payload, expected_status
):
    _service, backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _raw_post(
            url,
            path,
            token=token,
            body=json.dumps(payload),
            content_type="text/plain",
        )
    finally:
        stop_server(server, thread)
    assert status == expected_status
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {
            "expected": "application/json",
            "media_type": "text/plain",
            "rule": "request_content_type_unsupported",
        }
    ]
    assert backend.writes == 0
    assert backend.moves == 0


@pytest.mark.smoke
def test_protected_json_route_rejects_missing_media_type_before_parsing(tmp_path):
    _service, backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _raw_post(
            url,
            "/v1/commands/sections",
            token="agent-secret",
            body="not-json",
            content_type=None,
        )
    finally:
        stop_server(server, thread)
    assert status == 415
    assert result["errors"] == [
        {"expected": "application/json", "rule": "request_content_type_required"}
    ]
    assert backend.writes == 0


@pytest.mark.smoke
def test_application_json_with_charset_remains_accepted(tmp_path):
    _service, _backend, server, thread, url = _running(tmp_path)
    body = json.dumps(
        {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}}
    )
    try:
        status, result = _raw_post(
            url,
            "/v1/commands/sections",
            token="agent-secret",
            body=body,
            content_type="application/json; charset=utf-8",
        )
    finally:
        stop_server(server, thread)
    assert status == 200
    assert result["ok"] is True


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            '{"client":{"run_id":"' + RUN_ID + '","request_id":"' + REQUEST_ID
            + '"},"arguments":{"agent":"gpt","title":"A"},'
            '"arguments":{"agent":"gpt","title":"B"}}',
            "arguments",
        ),
        (
            '{"client":{"run_id":"' + RUN_ID + '","run_id":"' + OTHER_RUN_ID
            + '","request_id":"' + REQUEST_ID
            + '"},"arguments":{"agent":"gpt","title":"A"}}',
            "run_id",
        ),
        (
            '{"client":{"run_id":"' + RUN_ID + '","request_id":"' + REQUEST_ID
            + '"},"arguments":{"agent":"gpt","title":"A","title":"B"}}',
            "title",
        ),
    ],
)
def test_duplicate_json_keys_are_rejected_recursively_before_mutation(
    tmp_path, body, field
):
    service, backend, server, thread, url = _running(tmp_path)
    try:
        status, result = _raw_post(
            url,
            "/v1/action/create",
            token="action-secret",
            body=body,
            content_type="application/json",
        )
    finally:
        stop_server(server, thread)
    assert status == 200
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["errors"] == [
        {"field": field, "rule": "request_json_duplicate_key"}
    ]
    assert backend.writes == 0
    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0] == 0
    finally:
        conn.close()


def _logical_database_dump(path: Path) -> str:
    conn = initialize_database(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


@pytest.mark.smoke
def test_health_write_readiness_probe_is_logically_side_effect_free(tmp_path):
    service, _backend, _server, _thread, _url = _running(tmp_path)
    # The helper starts a server; stop it so this test exercises the service directly.
    stop_server(_server, _thread)
    before = _logical_database_dump(service.config.db_path)
    health = service.health()
    after = _logical_database_dump(service.config.db_path)
    assert health["ok"] is True
    assert health["database"] == {
        "ok": True,
        "schema_version": health["database"]["schema_version"],
        "write_ready": True,
    }
    assert after == before


@pytest.mark.smoke
def test_health_rejects_read_only_database_as_not_mutation_ready(monkeypatch, tmp_path):
    service, _backend, server, thread, _url = _running(tmp_path)
    stop_server(server, thread)
    conn = initialize_database(service.config.db_path)
    conn.close()

    def open_read_only(path):
        uri = f"file:{Path(path).resolve()}?mode=ro"
        readonly = sqlite3.connect(uri, uri=True, isolation_level=None)
        readonly.row_factory = sqlite3.Row
        readonly.execute("PRAGMA foreign_keys = ON")
        return readonly

    monkeypatch.setattr(application_module, "initialize_database", open_read_only)
    health = service.health()
    assert health["ok"] is False
    assert health["database"] == {
        "ok": False,
        "rule": "database_not_writable",
        "message": "workflow database is not mutation-ready",
        "write_ready": False,
    }


@pytest.mark.smoke
def test_health_reports_transient_writer_lock_without_calling_it_corruption(
    monkeypatch, tmp_path
):
    service, _backend, server, thread, _url = _running(tmp_path)
    stop_server(server, thread)
    monkeypatch.setattr(database_schema_module, "MIGRATION_BUSY_TIMEOUT_MS", 25)
    locker = initialize_database(service.config.db_path)
    locker.execute("BEGIN IMMEDIATE")
    try:
        health = service.health()
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    assert health["ok"] is False
    assert health["database"]["rule"] == "database_writer_lock"
    assert health["database"].get("write_ready") is not True
