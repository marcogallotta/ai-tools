from __future__ import annotations

import json
import logging
import subprocess
import sys
from contextlib import nullcontext
from datetime import timedelta
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from dish_pg.frontend_board_query import BoardReadUnavailable
from dish_service.frontend_contract import FRONTEND_CONTRACT_VERSION
import dish_service.frontend_local as frontend_local
from dish_service.frontend_board import FrontendBoardConfig
from dish_service.frontend_local import (
    FrontendLocalServer,
    LocalFrontendSettings,
    PostgresLocalBoardBackend,
)
from dish_service.frontend_tokens import CursorInvalid, CursorStale
from tests.support.thread_teardown import start_server_thread, stop_server

SECTION_ID = "r1s-" + "s" * 27
TASK_ID = "12345678-1234-5678-1234-567812345678"
_DEFAULT_HOST = object()


class FakeBackend:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.continuation_calls: list[tuple[str, str]] = []
        self.fail_bootstrap = False
        self.detail_calls: list[str] = []
        self.detail_outcome = "ok"

    def bootstrap(self):
        self.bootstrap_calls += 1
        if self.fail_bootstrap:
            raise RuntimeError("database-password=must-not-leak")
        return {
            "snapshot_id": "d1-snapshot",
            "page_size": 1,
            "sections": [
                {
                    "section_id": SECTION_ID,
                    "section_label": "Research Queue",
                    "continuity_id": "d1-continuity",
                    "cards": [
                        {
                            "task_id": TASK_ID,
                            "title": "[ready] Exact imported task",
                            "section_id": SECTION_ID,
                            "workflow_status": {"state": "no_active_operation"},
                            "attention_codes": ["isolated"],
                        }
                    ],
                    "next_cursor": "c1.next",
                }
            ],
            "notices": [{"code": "isolated", "task_id": TASK_ID, "severity": "warning"}],
        }

    def continuation(self, *, section_route_id: str, cursor: str):
        self.continuation_calls.append((section_route_id, cursor))
        if cursor == "invalid":
            raise CursorInvalid("raw cursor detail")
        if cursor == "stale":
            raise CursorStale("raw cursor detail")
        return {
            "section_id": section_route_id,
            "continuity_id": "d1-continuity",
            "cards": [],
            "next_cursor": None,
            "notices": [],
        }

    def detail(self, *, task_route_id: str):
        from dish_pg.frontend_detail_query import TaskDetailIneligible
        from dish_service.frontend_detail import DetailCapacityExceeded, TaskNotFound

        self.detail_calls.append(task_route_id)
        if self.detail_outcome == "not_found":
            raise TaskNotFound("opaque detail")
        if self.detail_outcome == "ineligible":
            raise TaskDetailIneligible("opaque detail")
        if self.detail_outcome == "capacity":
            raise DetailCapacityExceeded("opaque detail")
        if self.detail_outcome == "failed":
            raise RuntimeError("database-password=must-not-leak")
        return {
            "task_id": task_route_id,
            "title": "[ready] Exact imported task",
            "project_label": "Cooking",
            "section_label": "Research Queue",
            "destination_label": None,
            "workflow_status": {"state": "no_active_operation"},
            "attention_codes": ["isolated"],
            "body_presentation": {"state": "sanitized_html", "html": "<p>Canonical</p>"},
            "disclosures": [],
            "advisory": {"code": "workflow.none", "message": "No next step is currently available.", "perspective": "workflow", "invokable_by_frontend": False},
            "projection": None,
            "notices": [{"code": "isolated", "severity": "warning", "message": "Visible isolated task.", "target": {"type": "task", "route_identity": task_route_id}}],
        }


@pytest.fixture
def local_server(tmp_path: Path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<title>Dish local</title>\n", encoding="utf-8")
    (static / "asset.js").write_text("export const ok = true;\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("do not serve\n", encoding="utf-8")
    backend = FakeBackend()
    server = FrontendLocalServer(("127.0.0.1", 0), backend=backend, static_root=static)
    thread = start_server_thread(server, name="frontend-local-test")
    try:
        yield server, backend
    finally:
        stop_server(server, thread)


def request(
    server,
    method: str,
    path: str,
    *,
    contract: str | None = FRONTEND_CONTRACT_VERSION,
    host: str | None | object = _DEFAULT_HOST,
    duplicate_host: str | None = None,
):
    connection = HTTPConnection(*server.server_address, timeout=3)
    connection.putrequest(method, path, skip_host=True)
    if host is _DEFAULT_HOST:
        bound_host, bound_port = server.server_address[:2]
        host = f"{bound_host}:{bound_port}"
    if host is not None:
        connection.putheader("Host", host)
    if duplicate_host is not None:
        connection.putheader("Host", duplicate_host)
    if contract is not None:
        connection.putheader("X-Dish-Frontend-Contract", contract)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, headers_out, body


def json_body(body: bytes):
    return json.loads(body.decode("utf-8"))




def test_local_entry_point_imports_repository_from_any_working_directory(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "dish-frontend-local"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--database-url" in result.stdout


def test_postgresql_backend_sets_transaction_read_only_before_service(monkeypatch) -> None:
    events: list[str] = []

    class FakeSession:
        def begin(self):
            events.append("begin")
            return nullcontext()

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement):
            events.append(str(statement))

        def close(self):
            events.append("close")

    session = FakeSession()

    class FakeService:
        def __init__(self, query, **_kwargs):
            assert query is session
            events.append("service")

        def bootstrap(self):
            events.append("bootstrap")
            return {"ok": True}

    monkeypatch.setattr(frontend_local, "FrontendBoardQuery", lambda current: current)
    monkeypatch.setattr(frontend_local, "FrontendBoardService", FakeService)
    backend = PostgresLocalBoardBackend(
        lambda: session,
        token_secret=b"local-read-only-test-secret-32-bytes",
        config=FrontendBoardConfig(projection_delay=timedelta(minutes=15)),
    )

    assert backend.bootstrap() == {"ok": True}
    assert events == ["begin", "SET TRANSACTION READ ONLY", "service", "bootstrap", "close"]


def test_postgresql_driver_failure_is_classified_as_read_unavailable() -> None:
    from sqlalchemy.exc import OperationalError

    class FailingSession:
        def begin(self):
            return nullcontext()

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, _statement):
            raise OperationalError("SELECT 1", {}, RuntimeError("credential-detail"))

        def close(self):
            pass

    backend = PostgresLocalBoardBackend(
        lambda: FailingSession(),
        token_secret=b"local-driver-failure-test-secret-32",
        config=FrontendBoardConfig(projection_delay=timedelta(minutes=15)),
    )
    with pytest.raises(BoardReadUnavailable, match="local PostgreSQL read is unavailable"):
        backend.bootstrap()


def test_local_settings_reject_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LocalFrontendSettings(host="0.0.0.0")


def test_local_http_host_admission_is_fail_closed(local_server) -> None:
    server, backend = local_server
    bound_host, bound_port = server.server_address[:2]
    bad_hosts = (
        None,
        f"localhost:{bound_port}",
        f"{bound_host}:{bound_port + 1}",
        f"{bound_host}:not-a-port",
    )
    for host in bad_hosts:
        status, _, body = request(server, "GET", "/frontend/board", host=host)
        assert status == 400
        assert body == b"Bad request\n"
        assert backend.bootstrap_calls == 0

    status, _, body = request(
        server,
        "GET",
        "/frontend/board",
        duplicate_host=f"{bound_host}:{bound_port}",
    )
    assert status == 400
    assert body == b"Bad request\n"
    assert backend.bootstrap_calls == 0

    status, _, body = request(server, "GET", "/", contract=None, host=f"localhost:{bound_port}")
    assert status == 400
    assert body == b"Bad request\n"


def test_local_http_access_log_omits_route_and_cursor_values(local_server, caplog) -> None:
    server, _ = local_server
    caplog.set_level(logging.INFO, logger="dish.frontend.local")
    cursor = "opaque-cursor-must-not-appear"
    status, _, _ = request(
        server,
        "GET",
        f"/frontend/sections/{SECTION_ID}/tasks?cursor={cursor}",
    )
    assert status == 200
    assert "method=GET" in caplog.text
    assert "status=200" in caplog.text
    assert SECTION_ID not in caplog.text
    assert cursor not in caplog.text


def test_bootstrap_and_continuation_are_contract_bound_and_read_only(local_server) -> None:
    server, backend = local_server
    status, headers, body = request(server, "GET", "/frontend/board")
    assert status == 200
    assert headers["X-Dish-Frontend-Contract"] == FRONTEND_CONTRACT_VERSION
    assert json_body(body)["sections"][0]["cards"][0]["task_id"] == TASK_ID

    status, _, body = request(
        server,
        "GET",
        f"/frontend/sections/{SECTION_ID}/tasks?cursor=next-page",
    )
    assert status == 200
    assert json_body(body)["section_id"] == SECTION_ID
    assert backend.continuation_calls == [(SECTION_ID, "next-page")]

    status, _, body = request(server, "POST", "/frontend/board")
    assert status == 405
    assert json_body(body)["error"]["code"] == "request_invalid"
    assert backend.bootstrap_calls == 1


def test_bad_contract_and_bad_cursor_inputs_fail_before_application_dispatch(local_server) -> None:
    server, backend = local_server
    status, headers, body = request(server, "GET", "/frontend/board", contract=None)
    assert status == 403
    assert headers["X-Dish-Frontend-Contract"] == FRONTEND_CONTRACT_VERSION
    assert json_body(body)["error"]["code"] == "client_update_required"
    assert backend.bootstrap_calls == 0

    status, headers, body = request(server, "GET", "/frontend/board", contract="old-client")
    assert status == 403
    assert headers["X-Dish-Frontend-Contract"] == FRONTEND_CONTRACT_VERSION
    assert json_body(body)["error"]["code"] == "client_update_required"
    assert backend.bootstrap_calls == 0

    status, _, body = request(server, "GET", "/frontend/board?unexpected=1")
    assert status == 400
    assert json_body(body)["error"]["code"] == "request_invalid"
    assert backend.bootstrap_calls == 0

    status, _, body = request(server, "GET", f"/frontend/sections/{SECTION_ID}/tasks")
    assert status == 400
    assert json_body(body)["error"]["code"] == "request_invalid"
    assert backend.continuation_calls == []

    for cursor, expected_status, expected_code in (
        ("invalid", 400, "cursor_invalid"),
        ("stale", 409, "cursor_stale"),
    ):
        status, _, body = request(
            server,
            "GET",
            f"/frontend/sections/{SECTION_ID}/tasks?cursor={cursor}",
        )
        assert status == expected_status
        assert json_body(body)["error"]["code"] == expected_code
        assert b"raw cursor detail" not in body


def test_static_serving_rejects_traversal_and_has_spa_fallback(local_server) -> None:
    server, _ = local_server
    status, _, body = request(server, "GET", "/", contract=None)
    assert status == 200
    assert b"Dish local" in body

    status, _, body = request(server, "GET", "/board-view", contract=None)
    assert status == 200
    assert b"Dish local" in body

    status, _, body = request(server, "GET", "/%2e%2e/secret.txt", contract=None)
    assert status == 404
    assert b"do not serve" not in body


def test_unexpected_backend_failure_returns_closed_503_without_exception_text(local_server) -> None:
    server, backend = local_server
    backend.fail_bootstrap = True
    status, _, body = request(server, "GET", "/frontend/board")
    assert status == 503
    payload = json_body(body)
    assert payload["error"]["code"] == "internal_error"
    assert b"database-password" not in body


def test_task_detail_http_is_contract_bound_and_closed(local_server) -> None:
    server, backend = local_server
    status, headers, body = request(server, "GET", f"/frontend/tasks/{TASK_ID}")
    assert status == 200
    assert headers["X-Dish-Frontend-Contract"] == FRONTEND_CONTRACT_VERSION
    assert json_body(body)["task_id"] == TASK_ID
    assert backend.detail_calls == [TASK_ID]

    status, _, body = request(server, "GET", f"/frontend/tasks/{TASK_ID}?extra=1")
    assert status == 400
    assert json_body(body)["error"]["code"] == "request_invalid"
    assert backend.detail_calls == [TASK_ID]

    status, _, body = request(server, "POST", f"/frontend/tasks/{TASK_ID}")
    assert status == 405
    assert json_body(body)["error"]["code"] == "request_invalid"


def test_task_detail_http_maps_expected_failures_without_leakage(local_server) -> None:
    server, backend = local_server
    expected = {
        "not_found": (404, "task_not_found"),
        "ineligible": (409, "task_ineligible"),
        "capacity": (503, "detail_capacity_exceeded"),
        "failed": (503, "internal_error"),
    }
    for outcome, (status_expected, code_expected) in expected.items():
        backend.detail_outcome = outcome
        status, _, body = request(server, "GET", f"/frontend/tasks/{TASK_ID}")
        assert status == status_expected
        payload = json_body(body)
        assert payload["error"]["code"] == code_expected
        assert "password" not in body.decode("utf-8").lower()


def test_postgresql_detail_backend_uses_repeatable_read_and_presents_after_transaction(monkeypatch) -> None:
    events: list[str] = []

    class FakeSession:
        def begin(self):
            events.append("begin")
            class Context:
                def __enter__(self): return None
                def __exit__(self, *_args): events.append("transaction-close")
            return Context()
        def get_bind(self): return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        def execute(self, statement): events.append(str(statement))
        def close(self): events.append("session-close")

    session = FakeSession()
    class FakeDetailService:
        def __init__(self, query, **_kwargs):
            assert query is session
            events.append("detail-service")
        def capture(self, route):
            events.append(f"capture:{route}")
            return {"fact": True}
        def present(self, facts):
            assert facts == {"fact": True}
            events.append("present")
            return {"ok": True}

    monkeypatch.setattr(frontend_local, "FrontendDetailQuery", lambda current: current)
    monkeypatch.setattr(frontend_local, "FrontendDetailService", FakeDetailService)
    backend = PostgresLocalBoardBackend(
        lambda: session,
        token_secret=b"local-detail-test-secret-at-least-32",
        config=FrontendBoardConfig(projection_delay=timedelta(minutes=15)),
    )
    assert backend.detail(task_route_id=TASK_ID) == {"ok": True}
    assert events == [
        "begin",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "detail-service",
        f"capture:{TASK_ID}",
        "transaction-close",
        "present",
        "session-close",
    ]
