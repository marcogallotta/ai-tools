from __future__ import annotations

import http.client
import json
import socket
import threading
import uuid
from urllib.parse import urlsplit

import pytest

from dish_service import client as client_module
from dish_service.application import DishService
from dish_service.client import DishAdminServiceClient
from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer, build_server
from dish_service.leases import LeaseManager, ServicePrincipal
from dish_service.task_urls import task_gid_from_url
from dish_tool import admin_cli
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r42_service_foundation import _release_loader
from tests.test_dish_tool_step7_verification import Backend, TASK

OWNER_RUN = "11111111-1111-4111-8111-111111111111"
ADMIN_RUN = "22222222-2222-4222-8222-222222222222"
OTHER_ADMIN_RUN = "33333333-3333-4333-8333-333333333333"
START_REQUEST = "44444444-4444-4444-8444-444444444444"
EXPIRY_REQUEST = "55555555-5555-4555-8555-555555555555"
OTHER_REQUEST = "66666666-6666-4666-8666-666666666666"
TASK_GID = "123456789"


def _service(tmp_path, *, backend_factory=None, release_loader=None):
    backend = Backend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=backend_factory or (lambda: backend),
        release_loader=release_loader or _release_loader(honest),
    )
    return service, backend


def _start(service: DishService):
    principal = ServicePrincipal("agent", OWNER_RUN)
    result = service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": TASK_GID, "kind": "initial"},
        principal=principal,
        request_id=START_REQUEST,
    )
    assert result["ok"]
    return principal, result


def _admin(run_id: str = ADMIN_RUN):
    return ServicePrincipal("marco-admin", run_id)


def _running(tmp_path):
    service, backend = _service(tmp_path)
    server = build_server(service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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


def test_expire_exact_lease_releases_row_and_preserves_workflow(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    result = service.expire_lease(
        _admin(),
        lease_id=lease_id,
        reason="agent process died",
        request_id=EXPIRY_REQUEST,
    )

    assert result["ok"] is True
    assert result["allowed_actions"] == []
    assert result["submission_id"] == started["submission_id"]
    assert result["state"] == "open"
    assert result["data"]["outcome"] == "released"
    assert result["data"]["lease"]["release_reason"] == "admin expiry: agent process died"
    assert result["data"]["ownership_transferred"] is False

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        operation = conn.execute(
            "SELECT status,phase FROM operations WHERE operation_id=?",
            (started["submission_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert lease["released_at"] is not None
    assert lease["release_reason"] == "admin expiry: agent process died"
    assert tuple(operation) == ("open", "prepare_required")


def test_previous_eligible_run_can_reacquire_after_release(tmp_path):
    service, backend = _service(tmp_path)
    owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    prepared = service.execute_agent(
        "prepare",
        {
            "agent": "gpt",
            "model": "gpt-5.6-sol",
            "submission_id": started["submission_id"],
            "file_text": TASK,
        },
        principal=owner,
        request_id=OTHER_REQUEST,
    )

    assert prepared["ok"] is True
    assert backend.writes == 1


def test_exact_replay_never_touches_replacement_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    operation_id = started["submission_id"]
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert first["data"]["released"] is True

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(operation_id, owner)
    finally:
        conn.close()

    replay = service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert replay["data"]["request_replayed"] is True
    assert replay["data"]["lease"]["lease_id"] == old_lease_id

    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT * FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (operation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert active["lease_id"] == replacement["lease_id"]


def test_task_noop_replay_never_touches_later_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="cleanup", request_id=EXPIRY_REQUEST
    )
    no_op_request = str(uuid.uuid4())
    no_op = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="nothing active", request_id=no_op_request
    )
    assert no_op["data"]["outcome"] == "no_active_lease"

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(started["submission_id"], owner)
    finally:
        conn.close()

    replay = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="nothing active", request_id=no_op_request
    )
    assert replay["data"]["request_replayed"] is True
    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT lease_id FROM service_leases WHERE task_gid=? AND released_at IS NULL",
            (TASK_GID,),
        ).fetchone()
    finally:
        conn.close()
    assert active["lease_id"] == replacement["lease_id"]


def test_same_request_id_requires_same_admin_run(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert first["ok"]

    same = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    different = service.expire_lease(
        _admin(OTHER_ADMIN_RUN),
        lease_id=lease_id,
        reason="owner dead",
        request_id=EXPIRY_REQUEST,
    )
    assert same["data"]["request_replayed"] is True
    assert different["code"] == "CONFLICT"
    assert different["errors"][0]["rule"] == "service_request_identity_conflict"


@pytest.mark.parametrize("claim_live", [True, False])
def test_execution_claim_guard_uses_existing_liveness_and_preserves_claim(
    tmp_path, monkeypatch, claim_live
):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    operation_id = started["submission_id"]
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (operation_id, str(uuid.uuid4()), "prepare", "host", 123, "start", "now"),
        )
    finally:
        conn.close()
    monkeypatch.setattr(
        "dish_tool.operation_execution.process_identity_is_live",
        lambda _identity: claim_live,
    )

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )

    conn = initialize_database(service.config.db_path)
    try:
        active = conn.execute(
            "SELECT released_at FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert claim_count == 1
    if claim_live:
        assert result["code"] == "CONFLICT"
        assert result["retryable"] is True
        assert result["errors"][0]["rule"] == "operation_mutation_in_progress"
        assert active["released_at"] is None
    else:
        assert result["ok"] is True
        assert active["released_at"] is not None


def test_result_persistence_failure_rolls_back_release(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    def fail_completion(*_args, **_kwargs):
        raise RuntimeError("completion failed")

    monkeypatch.setattr("dish_service.application.complete_request", fail_completion)
    with pytest.raises(RuntimeError, match="completion failed"):
        service.expire_lease(
            _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
        )

    conn = initialize_database(service.config.db_path)
    try:
        lease = conn.execute(
            "SELECT released_at FROM service_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        request = conn.execute(
            "SELECT status FROM service_requests WHERE request_id=?", (EXPIRY_REQUEST,)
        ).fetchone()
    finally:
        conn.close()
    assert lease["released_at"] is None
    assert request["status"] == "pending"


def test_expiry_service_path_never_constructs_backend_or_release(tmp_path):
    seed, _backend = _service(tmp_path)
    _owner, started = _start(seed)
    lease_id = started["data"]["service_lease"]["lease_id"]

    def bomb(*_args, **_kwargs):
        raise AssertionError("workflow dependency constructed")

    service, _unused = _service(
        tmp_path, backend_factory=bomb, release_loader=bomb
    )
    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["ok"] is True


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
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 200
    assert first["data"]["lease"]["release_reason"] == "admin expiry: agent died"
    assert replay["data"]["request_replayed"] is True


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
        server.shutdown(); server.server_close(); thread.join(timeout=2)
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


class _RecordingAdminClient:
    run_id = ADMIN_RUN

    def __init__(self):
        self.calls = []

    def expire_lease(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "command": "expire-lease",
            "code": "OK",
            "task_gid": kwargs["task_gid"],
            "submission_id": None,
            "state": None,
            "retryable": False,
            "allowed_actions": [],
            "data": {"outcome": "no_active_lease", "request_id": kwargs["request_id"]},
            "errors": [],
        }


def test_cli_prints_and_flushes_replay_identity_before_dispatch(capsys):
    client = _RecordingAdminClient()
    status = admin_cli.main(
        [
            "expire-lease",
            "https://app.asana.com/0/987654321/123456789",
            "--reason",
            " agent died ",
            "--request-id",
            EXPIRY_REQUEST,
        ],
        application=client,
    )
    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == (
        f"expire-lease request_id={EXPIRY_REQUEST} run_id={ADMIN_RUN}\n"
    )
    assert client.calls == [
        {
            "lease_id": None,
            "task_gid": TASK_GID,
            "reason": "agent died",
            "request_id": EXPIRY_REQUEST,
        }
    ]


def test_cli_local_mode_rejects_before_local_dependencies(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DISH_MODE", "local")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", ADMIN_RUN)
    monkeypatch.setattr(
        admin_cli,
        "initialize_database",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database opened")),
    )
    monkeypatch.setattr(
        admin_cli,
        "AsanaBackend",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("backend created")),
    )
    status = admin_cli.main(
        ["expire-lease", TASK_GID, "--reason", "owner dead"]
    )
    result = json.loads(capsys.readouterr().out)
    assert status != 0
    assert result["errors"][0]["rule"] == "shared_service_required"


def test_service_canonicalizes_reason_before_request_hash(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]

    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="  owner dead  ", request_id=EXPIRY_REQUEST
    )
    replay = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )

    assert first["data"]["lease"]["release_reason"] == "admin expiry: owner dead"
    assert replay["data"]["request_replayed"] is True


def test_fresh_task_request_can_release_replacement_lease(tmp_path):
    service, _backend = _service(tmp_path)
    owner, started = _start(service)
    operation_id = started["submission_id"]
    old_lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=old_lease_id, reason="old owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    conn = initialize_database(service.config.db_path)
    try:
        replacement = LeaseManager(conn).acquire(operation_id, owner)
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), task_gid=TASK_GID, reason="release replacement", request_id=OTHER_REQUEST
    )
    assert result["data"]["outcome"] == "released"
    assert result["data"]["lease"]["lease_id"] == replacement["lease_id"]


def test_already_released_exact_target_preserves_original_release(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    first = service.expire_lease(
        _admin(), lease_id=lease_id, reason="first reason", request_id=EXPIRY_REQUEST
    )

    second = service.expire_lease(
        _admin(), lease_id=lease_id, reason="second reason", request_id=OTHER_REQUEST
    )

    assert second["ok"] is True
    assert second["data"]["outcome"] == "already_released"
    assert second["data"]["released"] is False
    assert second["data"]["lease"]["released_at"] == first["data"]["lease"]["released_at"]
    assert second["data"]["lease"]["release_reason"] == "admin expiry: first reason"


def test_unknown_exact_lease_returns_canonical_not_found(tmp_path):
    service, _backend = _service(tmp_path)
    unknown = str(uuid.uuid4())
    result = service.expire_lease(
        _admin(), lease_id=unknown, reason="operator lookup", request_id=EXPIRY_REQUEST
    )

    assert result["ok"] is False
    assert result["code"] == "NOT_FOUND"
    assert result["task_gid"] is None
    assert result["submission_id"] is None
    assert result["state"] is None
    assert result["allowed_actions"] == []
    assert result["data"]["request_id"] == EXPIRY_REQUEST
    assert result["errors"] == [{"rule": "service_lease_not_found", "lease_id": unknown}]


def test_active_lease_on_terminal_operation_can_be_released(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            "UPDATE operations SET status='completed', phase='terminal', completed_at='now', "
            "terminal_outcome='test' WHERE operation_id=?",
            (started["submission_id"],),
        )
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="terminal cleanup", request_id=EXPIRY_REQUEST
    )

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["data"]["outcome"] == "released"


@pytest.mark.parametrize(
    ("principal", "reason"),
    [
        (ServicePrincipal("other-admin", ADMIN_RUN), "owner dead"),
        (_admin(), "different reason"),
    ],
)
def test_request_identity_conflicts_on_principal_or_reason_change(
    tmp_path, principal, reason
):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    assert service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )["ok"]

    conflict = service.expire_lease(
        principal, lease_id=lease_id, reason=reason, request_id=EXPIRY_REQUEST
    )
    assert conflict["code"] == "CONFLICT"
    assert conflict["errors"][0]["rule"] == "service_request_identity_conflict"


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
        server.shutdown(); server.server_close(); thread.join(timeout=2)

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
        server.shutdown(); server.server_close(); thread.join(timeout=2)
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
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert result["ok"] is True
    assert result["data"]["outcome"] == "released"


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
        server.shutdown(); server.server_close(); thread.join(timeout=2)


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


def test_duplicate_request_id_concurrency_returns_one_release_and_one_replay(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    lease_id = started["data"]["service_lease"]["lease_id"]
    barrier = threading.Barrier(2)
    results = []

    def worker():
        barrier.wait()
        results.append(
            service.expire_lease(
                _admin(),
                lease_id=lease_id,
                reason="owner dead",
                request_id=EXPIRY_REQUEST,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for item in threads:
        item.start()
    for item in threads:
        item.join(timeout=5)

    assert len(results) == 2
    assert all(result["ok"] for result in results)
    assert sum(bool(result["data"].get("request_replayed")) for result in results) == 1
    assert {result["data"]["lease"]["lease_id"] for result in results} == {lease_id}


def test_cli_malformed_target_and_invalid_run_id_fail_before_dependencies(
    monkeypatch, capsys
):
    def bomb(*_args, **_kwargs):
        raise AssertionError("local workflow dependency constructed")

    monkeypatch.setattr(admin_cli, "initialize_database", bomb)
    monkeypatch.setattr(admin_cli, "AsanaBackend", bomb)

    status = admin_cli.main(["expire-lease", "not-a-target", "--reason", "x"])
    malformed = json.loads(capsys.readouterr().out)
    assert status != 0
    assert malformed["errors"][0]["rule"] == "uuid_identifier_required"

    monkeypatch.setenv("DISH_MODE", "service")
    monkeypatch.setenv("DISH_SERVICE_URL", "http://dish.invalid")
    monkeypatch.setenv("DISH_CLIENT_RUN_ID", "bad")
    monkeypatch.setenv("DISH_ADMIN_TOKEN", "admin-secret")
    status = admin_cli.main(["expire-lease", TASK_GID, "--reason", "x"])
    invalid_run = json.loads(capsys.readouterr().out)
    assert status != 0
    assert invalid_run["errors"][0]["rule"] == "uuid_identifier_required"


def test_foreign_host_execution_claim_fails_closed(tmp_path):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    operation_id = started["submission_id"]
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation_id,
                str(uuid.uuid4()),
                "prepare",
                "definitely-another-host",
                123,
                "start",
                "now",
            ),
        )
    finally:
        conn.close()

    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "operation_mutation_in_progress"


def test_permission_denied_process_check_fails_closed(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    _owner, started = _start(service)
    operation_id = started["submission_id"]
    lease_id = started["data"]["service_lease"]["lease_id"]
    conn = initialize_database(service.config.db_path)
    try:
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation_id,
                str(uuid.uuid4()),
                "prepare",
                socket.gethostname(),
                123,
                "fallback:123",
                "now",
            ),
        )
    finally:
        conn.close()

    monkeypatch.setattr("dish_tool.recovery._linux_process_start", lambda _pid: None)

    def denied(_pid, _signal):
        raise PermissionError("denied")

    monkeypatch.setattr("dish_tool.recovery.os.kill", denied)
    result = service.expire_lease(
        _admin(), lease_id=lease_id, reason="owner dead", request_id=EXPIRY_REQUEST
    )
    assert result["code"] == "CONFLICT"
    assert result["errors"][0]["rule"] == "operation_mutation_in_progress"


def test_action_listener_does_not_expose_expiry_route(tmp_path):
    service, _backend = _service(tmp_path)
    server = DishHTTPServer(("127.0.0.1", 0), service, surface_mode="action")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
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
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert status == 404
    assert result == {"ok": False, "error": "not_found"}
