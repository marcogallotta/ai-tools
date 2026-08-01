from __future__ import annotations

import http.client
import json
import threading
import uuid
from urllib.parse import urlsplit

import pytest

from dish_service import client as client_module
from dish_service.client import DishAdminServiceClient
from dish_service.http import DishHTTPServer, build_server
from dish_service.leases import LeaseManager
from dish_service.task_urls import task_gid_from_url
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.thread_teardown import join_thread, start_server_thread, stop_server
from tests.support.lease_expiry import ADMIN_RUN, EXPIRY_REQUEST, TASK_GID, _admin, _service, _start


def _running(tmp_path):
    service, backend = _service(tmp_path)
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    return service, backend, server, thread, f"http://{host}:{port}"


def _post(url, path, payload, token="admin-secret"):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        body = json.dumps(payload)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.smoke
def test_http_route_trims_reason_before_request_hash(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    payload = {
        "lease_id": lease_id,
        "reason": "  agent died  ",
        "client": {"run_id": ADMIN_RUN, "request_id": EXPIRY_REQUEST},
    }
    try:
        status, first = _post(url, "/v1/admin/leases/expire", payload)
        payload["reason"] = "agent died"
        _status, replay = _post(url, "/v1/admin/leases/expire", payload)
    finally:
        stop_server(server, thread)
    assert status == 200
    assert first["data"]["lease"]["release_reason"] == "admin expiry: agent died"
    assert replay["data"]["request_replayed"] is True


@pytest.mark.smoke
def test_http_validation_boundary_journals_target_reason_but_not_shape(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    invalid_request = str(uuid.uuid4())
    shape_request = str(uuid.uuid4())
    try:
        _status, invalid = _post(
            url,
            "/v1/admin/leases/expire",
            {
                "lease_id": "bad",
                "reason": ["not", "a", "string"],
                "client": {"run_id": ADMIN_RUN, "request_id": invalid_request},
            },
        )
        status, shape = _post(
            url,
            "/v1/admin/leases/expire",
            {
                "task_gid": TASK_GID,
                "reason": "x",
                "extra": True,
                "client": {"run_id": ADMIN_RUN, "request_id": shape_request},
            },
        )
    finally:
        stop_server(server, thread)
    assert invalid["code"] == "INVALID_ARGUMENT"
    assert status == 400
    assert shape["errors"][0]["rule"] == "request_field_unexpected"
    conn = initialize_database(service.config.db_path)
    try:
        rows = conn.execute(
            "SELECT request_id FROM service_requests WHERE request_id IN (?,?) ORDER BY request_id",
            (invalid_request, shape_request),
        ).fetchall()
    finally:
        conn.close()
    assert [row["request_id"] for row in rows] == [invalid_request]


@pytest.mark.smoke
def test_task_url_parser_is_narrow_and_deterministic():
    assert task_gid_from_url(
        "https://APP.ASANA.COM/0/987654321/123456789"
    ) == TASK_GID
    assert task_gid_from_url(
        "https://app.asana.com/1/111/project/222/task/123456789"
    ) == TASK_GID
    for value in (
        "http://app.asana.com/0/987654321/123456789",
        "https://app.asana.com/0/987654321/123456789/f",
        "https://app.asana.com/0/987654321/123456789?x=1",
        "https://user@app.asana.com/0/987654321/123456789",
        "https://app.asana.com:444/0/987654321/123456789",
        "https://app.asana.com/0/987654321/%31%32%33",
    ):
        with pytest.raises(DishRuleError):
            task_gid_from_url(value)


class _FakeSocket:
    def settimeout(self, value):
        self.timeout = value


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload


class _BaseFakeConnection:
    def __init__(self, host, port, timeout=None):
        self.sock = _FakeSocket()
        self.closed = False

    def connect(self):
        pass

    def request(self, *_args, **_kwargs):
        pass

    def close(self):
        self.closed = True


@pytest.mark.smoke
@pytest.mark.parametrize("failure", ["disconnect", "invalid-json", "noncanonical"])
def test_expire_client_maps_post_dispatch_failures_to_exact_replay_envelope(
    monkeypatch, failure
):
    class FakeConnection(_BaseFakeConnection):
        def getresponse(self):
            if failure == "disconnect":
                raise http.client.RemoteDisconnected("lost")
            if failure == "invalid-json":
                return _FakeResponse(b"not-json")
            return _FakeResponse(b'{"ok":true}')

    monkeypatch.setattr(client_module.http.client, "HTTPConnection", FakeConnection)
    client = DishAdminServiceClient(
        "http://dish.invalid", token="admin-secret", run_id=ADMIN_RUN
    )
    result = client.expire_lease(
        task_gid=TASK_GID, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["retryable"] is False
    assert result["task_gid"] == TASK_GID
    assert result["data"] == {
        "message": "the service may have processed the lease-expiry request",
        "request_id": EXPIRY_REQUEST,
        "run_id": ADMIN_RUN,
        "request_replay_required": True,
        "required_next_action": "retry_exact_request",
    }
    assert result["errors"] == [{"rule": "service_response_ambiguous"}]


@pytest.mark.smoke
def test_expire_client_keeps_connect_failure_nonambiguous(monkeypatch):
    class FakeConnection(_BaseFakeConnection):
        def connect(self):
            raise ConnectionRefusedError("no service")

    monkeypatch.setattr(client_module.http.client, "HTTPConnection", FakeConnection)
    client = DishAdminServiceClient(
        "http://dish.invalid", token="admin-secret", run_id=ADMIN_RUN
    )
    with pytest.raises(DishRuleError) as exc:
        client.expire_lease(
            task_gid=TASK_GID, reason="owner dead", request_id=EXPIRY_REQUEST
        )
    assert exc.value.code == "BACKEND_REJECTED"
    assert exc.value.rule == "service_unavailable"


@pytest.mark.smoke
def test_real_http_client_accepts_canonical_expiry_response(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    _owner, started = _start(service)
    client = DishAdminServiceClient(url, token="admin-secret", run_id=ADMIN_RUN)
    try:
        result = client.expire_lease(
            lease_id=started["data"]["service_lease"]["lease_id"],
            reason="owner dead",
            request_id=EXPIRY_REQUEST,
        )
    finally:
        stop_server(server, thread)
    assert result["ok"] is True
    assert result["data"]["outcome"] == "released"


@pytest.mark.smoke
def test_committed_lost_response_exact_retry_does_not_release_replacement(
    tmp_path, monkeypatch
):
    service, _backend, server, thread, url = _running(tmp_path)
    owner, started = _start(service)
    operation_id = started["submission_id"]
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    real_connection = client_module.http.client.HTTPConnection

    class LostResponseConnection:
        def __init__(self, host, port, timeout=None):
            self.inner = real_connection(host, port, timeout=timeout)
            self.sock = None

        def connect(self):
            self.inner.connect()
            self.sock = self.inner.sock

        def request(self, *args, **kwargs):
            return self.inner.request(*args, **kwargs)

        def getresponse(self):
            response = self.inner.getresponse()
            response.read()
            raise http.client.RemoteDisconnected("response lost after commit")

        def close(self):
            self.inner.close()

    client = DishAdminServiceClient(url, token="admin-secret", run_id=ADMIN_RUN)
    try:
        monkeypatch.setattr(
            client_module.http.client, "HTTPConnection", LostResponseConnection
        )
        ambiguous = client.expire_lease(
            lease_id=old_lease_id,
            reason="owner dead",
            request_id=EXPIRY_REQUEST,
        )
        assert ambiguous["code"] == "BACKEND_UNCERTAIN"

        conn = initialize_database(service.config.db_path)
        try:
            replacement = LeaseManager(conn).acquire(operation_id, owner)
        finally:
            conn.close()

        monkeypatch.setattr(client_module.http.client, "HTTPConnection", real_connection)
        replay = client.expire_lease(
            lease_id=old_lease_id,
            reason="owner dead",
            request_id=EXPIRY_REQUEST,
        )
        assert replay["data"]["request_replayed"] is True

        conn = initialize_database(service.config.db_path)
        try:
            active = conn.execute(
                "SELECT lease_id FROM service_leases WHERE operation_id=? AND released_at IS NULL",
                (operation_id,),
            ).fetchone()
        finally:
            conn.close()
        assert active["lease_id"] == replacement["lease_id"]
    finally:
        stop_server(server, thread)


@pytest.mark.smoke
def test_settimeout_failure_is_known_predispatch_failure(monkeypatch):
    class FailingSocket:
        def settimeout(self, _value):
            raise OSError("socket configuration failed")

    class FakeConnection(_BaseFakeConnection):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout=timeout)
            self.sock = FailingSocket()

    monkeypatch.setattr(client_module.http.client, "HTTPConnection", FakeConnection)
    client = DishAdminServiceClient(
        "http://dish.invalid", token="admin-secret", run_id=ADMIN_RUN
    )
    with pytest.raises(DishRuleError) as exc:
        client.expire_lease(
            task_gid=TASK_GID, reason="owner dead", request_id=EXPIRY_REQUEST
        )
    assert exc.value.rule == "service_unavailable"


@pytest.mark.smoke
def test_http_nonstring_reason_and_target_cardinality_are_journalled(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    reason_request = str(uuid.uuid4())
    target_request = str(uuid.uuid4())
    try:
        _status, bad_reason = _post(
            url,
            "/v1/admin/leases/expire",
            {
                "task_gid": TASK_GID,
                "reason": ["not", "a", "string"],
                "client": {"run_id": ADMIN_RUN, "request_id": reason_request},
            },
        )
        _status, bad_target = _post(
            url,
            "/v1/admin/leases/expire",
            {
                "lease_id": str(uuid.uuid4()),
                "task_gid": TASK_GID,
                "reason": "x",
                "client": {"run_id": ADMIN_RUN, "request_id": target_request},
            },
        )
    finally:
        stop_server(server, thread)

    assert bad_reason["errors"][0]["rule"] == "lease_expiry_reason_invalid"
    assert bad_target["errors"][0]["rule"] == "lease_expiry_target_invalid"
    conn = initialize_database(service.config.db_path)
    try:
        rows = conn.execute(
            "SELECT request_id,status FROM service_requests WHERE request_id IN (?,?)",
            (reason_request, target_request),
        ).fetchall()
    finally:
        conn.close()
    assert {(row["request_id"], row["status"]) for row in rows} == {
        (reason_request, "completed"),
        (target_request, "completed"),
    }


@pytest.mark.smoke
def test_http_invalid_client_identity_is_not_journalled(tmp_path):
    service, _backend, server, thread, url = _running(tmp_path)
    request_id = str(uuid.uuid4())
    try:
        status, result = _post(
            url,
            "/v1/admin/leases/expire",
            {
                "task_gid": TASK_GID,
                "reason": "x",
                "client": {"run_id": "not-a-uuid", "request_id": request_id},
            },
        )
    finally:
        stop_server(server, thread)
    assert status == 400
    assert result["errors"][0]["rule"] == "uuid_identifier_required"
    conn = initialize_database(service.config.db_path)
    try:
        stored = conn.execute(
            "SELECT 1 FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    finally:
        conn.close()
    assert stored is None


@pytest.mark.smoke
def test_action_listener_does_not_expose_expiry_route(tmp_path):
    service, _backend = _service(tmp_path)
    server = DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action")
    thread = start_server_thread(server, daemon=True, name="thread")
    host, port = server.server_address
    try:
        status, result = _post(
            f"http://{host}:{port}",
            "/v1/admin/leases/expire",
            {
                "task_gid": TASK_GID,
                "reason": "x",
                "client": {"run_id": ADMIN_RUN, "request_id": EXPIRY_REQUEST},
            },
            token="action-secret",
        )
    finally:
        stop_server(server, thread)
    assert status == 404
    assert result == {"ok": False, "error": "not_found"}
