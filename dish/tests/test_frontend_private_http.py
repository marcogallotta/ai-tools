from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from dish_service.config import ServiceConfig
from dish_service.frontend_auth import (
    FrontendAuthFailure,
    FrontendPrincipal,
    LoginResult,
    SessionBootstrap,
)
from dish_service.frontend_http import dispatch_get
from dish_service.http import DishHTTPServer

pytestmark = pytest.mark.smoke

TOKEN = "A" * 43
CSRF = "B" * 43
CONTRACT = "dish-frontend-v1"


class FakeAuth:
    def __init__(self) -> None:
        self.login_calls = 0
        self.validate_calls = 0
        self.logout_calls = 0
        self.fail_validate_at: int | None = None
        self.presented_tokens: list[str | None] = []
        self.principal = FrontendPrincipal(
            "session-id",
            datetime.now(timezone.utc) + timedelta(days=7),
            1,
        )

    def login(self, *, password: str, peer: str, presented_token=None) -> LoginResult:
        self.login_calls += 1
        self.presented_tokens.append(presented_token)
        assert password == "correct horse battery staple"
        assert peer == "127.0.0.1"
        return LoginResult(TOKEN, self.principal)

    def bootstrap(self, token: str) -> SessionBootstrap:
        assert token == TOKEN
        return SessionBootstrap(self.principal, CSRF, 604800)

    def validate(self, token: str, **_kwargs) -> FrontendPrincipal:
        assert token == TOKEN
        self.validate_calls += 1
        if self.fail_validate_at == self.validate_calls:
            raise FrontendAuthFailure("session_revoked", "The session is no longer valid.")
        return self.principal

    def logout(self, token: str, *, csrf: str) -> None:
        assert token == TOKEN
        assert csrf == CSRF
        self.logout_calls += 1


class FakeService:
    def __init__(self, tmp_path: Path) -> None:
        self.config = ServiceConfig(
            db_path=tmp_path / "legacy.sqlite3",
            honest_root=tmp_path,
            agent_token="agent-token-long-enough",
            admin_token="admin-token-long-enough",
            action_token="action-token-long-enough",
        )


class FakeRuntime:
    def __init__(self, tmp_path: Path, *, mode: str = "private-fixture") -> None:
        self.settings = SimpleNamespace(origin="https://dish.example.test")
        self.browser_runtime_mode = mode
        self.auth = FakeAuth()
        self.static_root = tmp_path / "dist"
        self.static_root.mkdir(parents=True)
        (self.static_root / "index.html").write_text(
            '<meta name="dish-runtime-mode" content="fixture"><main id="app"></main>\n',
            encoding="utf-8",
        )
        fixtures = self.static_root / "fixtures"
        fixtures.mkdir()
        (fixtures / "board.json").write_text('{"fixture":true}\n', encoding="utf-8")
        self.board_calls = 0

    def board(self):
        self.board_calls += 1
        return {"kind": "board"}

    def continuation(self, **_kwargs):
        return {"kind": "page"}

    def detail(self, **_kwargs):
        return {"kind": "detail"}

    def openapi_document(self):
        return {"openapi": "3.1.0"}


@pytest.fixture
def private_server(tmp_path: Path):
    runtime = FakeRuntime(tmp_path)
    server = DishHTTPServer(("127.0.0.1", 0), FakeService(tmp_path), surface_mode="private", frontend_runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, runtime
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(
    server,
    method: str,
    path: str,
    *,
    body=None,
    cookie: str | None = None,
    contract: str | None = CONTRACT,
    state_changing: bool = False,
    extra_headers: list[tuple[str, str]] | None = None,
):
    connection = HTTPConnection(*server.server_address, timeout=3)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", "dish.example.test")
    if contract is not None:
        connection.putheader("X-Dish-Frontend-Contract", contract)
    if cookie is not None:
        connection.putheader("Cookie", f"__Host-dish_frontend_session={cookie}")
    if state_changing:
        connection.putheader("Origin", "https://dish.example.test")
        connection.putheader("Sec-Fetch-Site", "same-origin")
        connection.putheader("Sec-Fetch-Mode", "cors")
        connection.putheader("Sec-Fetch-Dest", "empty")
    raw = None if body is None else json.dumps(body).encode()
    if raw is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(raw)))
    for name, value in extra_headers or []:
        connection.putheader(name, value)
    connection.endheaders(raw)
    response = connection.getresponse()
    payload = response.read()
    headers = response.getheaders()
    status = response.status
    connection.close()
    return status, headers, payload


def header_values(headers, name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name.lower()]


def api(body: bytes):
    return json.loads(body.decode())


def test_unauthenticated_html_redirects_and_authenticated_fixture_shell_is_served(private_server) -> None:
    server, _ = private_server
    status, headers, body = request(server, "GET", "/tasks/r1t-AAAAAAAAAAAAAAAAAAAAAAAAAAA/example", contract=None)
    assert status == 303
    assert header_values(headers, "Location")[0].startswith("/login?return=rt1.")
    assert body == b""

    status, headers, body = request(server, "GET", "/", cookie=TOKEN, contract=None)
    assert status == 200
    assert b'content="private-fixture"' in body
    assert header_values(headers, "Cache-Control") == ["no-store"]
    assert header_values(headers, "Content-Security-Policy")


def test_login_rejects_origin_before_password_and_sets_one_hardened_cookie(private_server) -> None:
    server, runtime = private_server
    status, _, body = request(server, "POST", "/frontend/login", body={"password": "correct horse battery staple"})
    assert status == 403
    assert api(body)["error"]["code"] == "origin_rejected"
    assert runtime.auth.login_calls == 0

    status, headers, body = request(
        server,
        "POST",
        "/frontend/login",
        body={"password": "correct horse battery staple"},
        state_changing=True,
    )
    assert status == 200
    assert api(body) == {}
    cookies = header_values(headers, "Set-Cookie")
    assert len(cookies) == 1
    assert cookies[0] == (
        f"__Host-dish_frontend_session={TOKEN}; Max-Age=604800; Path=/; "
        "Secure; HttpOnly; SameSite=Strict"
    )
    assert runtime.auth.login_calls == 1


def test_login_discards_one_malformed_session_cookie_before_password_work(private_server) -> None:
    server, runtime = private_server
    status, headers, body = request(
        server,
        "POST",
        "/frontend/login",
        body={"password": "correct horse battery staple"},
        cookie="not-a-valid-session-token",
        state_changing=True,
    )
    assert status == 200
    assert api(body) == {}
    assert runtime.auth.presented_tokens == [None]
    assert len(header_values(headers, "Set-Cookie")) == 1


def test_session_bootstrap_and_logout_are_contract_bound_without_stale_cookie_clear(private_server) -> None:
    server, runtime = private_server
    status, _, body = request(server, "GET", "/frontend/session", cookie=TOKEN)
    assert status == 200
    assert set(api(body)) == {"expires_at", "remaining_seconds", "csrf_proof"}

    status, headers, body = request(
        server,
        "POST",
        "/frontend/logout",
        body={},
        cookie=TOKEN,
        state_changing=True,
        extra_headers=[("X-Dish-CSRF", CSRF)],
    )
    assert status == 200
    assert api(body) == {}
    assert header_values(headers, "Set-Cookie") == []
    assert runtime.auth.logout_calls == 1


def test_protected_payload_is_withheld_when_final_session_check_fails(private_server) -> None:
    server, runtime = private_server
    runtime.auth.fail_validate_at = 2
    status, _, body = request(server, "GET", "/frontend/board", cookie=TOKEN)
    assert status == 401
    assert api(body)["error"]["code"] == "session_revoked"
    assert b'"kind":"board"' not in body
    assert runtime.board_calls == 1


def test_request_header_bound_fails_before_login_dispatch(private_server) -> None:
    server, runtime = private_server
    extras = [(f"X-Filler-{index}", "x") for index in range(60)]
    status, _, body = request(
        server,
        "POST",
        "/frontend/login",
        body={"password": "correct horse battery staple"},
        state_changing=True,
        extra_headers=extras,
    )
    assert status == 422
    assert api(body)["error"]["code"] == "request_invalid"
    assert runtime.auth.login_calls == 0


def test_action_listener_returns_404_for_frontend_routes(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path)
    server = DishHTTPServer(("127.0.0.1", 0), FakeService(tmp_path), surface_mode="action", frontend_runtime=runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = request(server, "GET", "/login", contract=None)
        assert status == 404
        assert api(body)["error"] == "not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_fixture_payload_requires_session_and_is_disabled_in_postgresql_mode(private_server, tmp_path: Path) -> None:
    server, _ = private_server
    status, _, body = request(server, "GET", "/fixtures/board.json", contract=None)
    assert status == 401
    assert api(body)["error"]["code"] == "auth_required"

    status, _, body = request(server, "GET", "/js/%2e%2e/fixtures/board.json", contract=None)
    assert status == 404
    assert body == b"Not found\n"

    status, _, body = request(server, "GET", "/fixtures/board.json", cookie=TOKEN, contract=None)
    assert status == 200
    assert api(body) == {"fixture": True}

    runtime = FakeRuntime(tmp_path / "postgresql", mode="private-postgresql")
    pg_server = DishHTTPServer(("127.0.0.1", 0), FakeService(tmp_path), surface_mode="private", frontend_runtime=runtime)
    thread = threading.Thread(target=pg_server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = request(pg_server, "GET", "/fixtures/board.json", cookie=TOKEN, contract=None)
        assert status == 404
        assert body == b"Not found\n"
    finally:
        pg_server.shutdown()
        pg_server.server_close()
        thread.join(timeout=3)


def test_ambiguous_security_headers_fail_closed_before_dispatch(private_server) -> None:
    server, runtime = private_server
    status, _, body = request(
        server,
        "POST",
        "/frontend/login",
        body={"password": "correct horse battery staple"},
        state_changing=True,
        extra_headers=[("Origin", "https://dish.example.test")],
    )
    assert status == 403
    assert api(body)["error"]["code"] == "origin_rejected"
    assert runtime.auth.login_calls == 0

    status, _, body = request(
        server,
        "GET",
        "/frontend/session",
        cookie=TOKEN,
        extra_headers=[("X-Dish-Frontend-Contract", CONTRACT)],
    )
    assert status == 403
    assert api(body)["error"]["code"] == "client_update_required"

    status, _, body = request(
        server,
        "POST",
        "/frontend/logout",
        body={},
        cookie=TOKEN,
        state_changing=True,
        extra_headers=[("X-Dish-CSRF", CSRF), ("X-Dish-CSRF", "C" * 43)],
    )
    assert status == 403
    assert api(body)["error"]["code"] == "csrf_rejected"
    assert runtime.auth.logout_calls == 0


def test_duplicate_host_is_origin_rejected(private_server) -> None:
    server, runtime = private_server
    status, _, body = request(
        server,
        "POST",
        "/frontend/login",
        body={"password": "correct horse battery staple"},
        state_changing=True,
        extra_headers=[("Host", "dish.example.test")],
    )
    assert status == 403
    assert api(body)["error"]["code"] == "origin_rejected"
    assert runtime.auth.login_calls == 0
