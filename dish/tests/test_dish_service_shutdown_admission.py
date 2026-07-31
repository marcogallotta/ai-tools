from __future__ import annotations

import http.client
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import pytest

from dish_service import __main__ as service_main
from dish_service.config import ServiceConfig
from dish_service.http import DishHTTPServer


@dataclass
class _HealthService:
    config: ServiceConfig
    entered: threading.Event | None = None
    release: threading.Event | None = None
    calls: int = 0

    def health(self) -> dict[str, object]:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=3)
        return {"ok": True, "startup_ready": True}


def _config(tmp_path: Path, *, request_timeout_seconds: float = 5.0) -> ServiceConfig:
    return ServiceConfig(
        db_path=tmp_path / "dish.db",
        honest_root=tmp_path,
        port=0,
        action_port=0,
        request_timeout_seconds=request_timeout_seconds,
        agent_token="agent-secret-123",
        admin_token="admin-secret-456",
        action_token="action-secret-789",
    )


def _running_server(tmp_path: Path, service: _HealthService | None = None):
    actual_service = service or _HealthService(_config(tmp_path))
    server = DishHTTPServer(("127.0.0.1", 0), actual_service, surface_mode="private")
    stop_event = threading.Event()
    server.attach_stop_event(stop_event)
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    return actual_service, server, stop_event, thread


def _stop(server: DishHTTPServer, stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    service_main._shutdown_servers((server,), (thread,), started_count=1)
    assert not thread.is_alive()


def test_normal_response_closes_loopback_connection(tmp_path):
    _service, server, stop_event, thread = _running_server(tmp_path)
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read()
        assert response.getheader("Connection") == "close"
        assert response.will_close is True
    finally:
        connection.close()
        _stop(server, stop_event, thread)


@pytest.mark.flake_stress
def test_shutdown_gate_drops_request_on_existing_unadmitted_connection(tmp_path):
    service, server, stop_event, thread = _running_server(tmp_path)
    connection = socket.create_connection(server.server_address, timeout=2)
    try:
        stop_event.set()
        connection.sendall(
            b"GET /health HTTP/1.1\r\nHost: dish.invalid\r\nConnection: close\r\n\r\n"
        )
        try:
            received = connection.recv(4096)
        except (ConnectionResetError, OSError):
            received = b""
        assert received == b""
        assert service.calls == 0
    finally:
        connection.close()
        service_main._shutdown_servers((server,), (thread,), started_count=1)
        assert not thread.is_alive()


@pytest.mark.flake_stress
def test_shutdown_wakes_idle_preaccepted_connection_without_request_timeout(tmp_path):
    service = _HealthService(_config(tmp_path, request_timeout_seconds=30.0))
    _service, server, stop_event, thread = _running_server(tmp_path, service)
    accepted = threading.Event()
    real_get_request = server.get_request

    def observed_get_request():
        result = real_get_request()
        accepted.set()
        return result

    server.get_request = observed_get_request  # type: ignore[method-assign]
    connection = socket.create_connection(server.server_address, timeout=2)
    try:
        assert accepted.wait(timeout=2), "server did not accept idle connection"

        started = time.monotonic()
        stop_event.set()
        service_main._shutdown_servers((server,), (thread,), started_count=1)
        elapsed = time.monotonic() - started

        assert elapsed < 1
        assert not thread.is_alive()
        assert service.calls == 0
    finally:
        connection.close()


@pytest.mark.flake_stress
def test_shutdown_drains_request_that_crossed_admission_boundary(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    service = _HealthService(_config(tmp_path), entered=entered, release=release)
    _service, server, stop_event, thread = _running_server(tmp_path, service)
    result: dict[str, object] = {}

    def request_health() -> None:
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            result["status"] = response.status
            result["body"] = response.read()
        finally:
            connection.close()

    request_thread = threading.Thread(target=request_health, daemon=False)
    request_thread.start()
    assert entered.wait(timeout=2)

    stop_event.set()
    shutdown_started = threading.Event()
    shutdown_finished = threading.Event()
    shutdown_errors: list[BaseException] = []

    def shutdown():
        shutdown_started.set()
        try:
            service_main._shutdown_servers((server,), (thread,), started_count=1)
        except BaseException as exc:  # pragma: no cover - surfaced below
            shutdown_errors.append(exc)
        finally:
            shutdown_finished.set()

    shutdown_thread = threading.Thread(target=shutdown, daemon=False)
    shutdown_thread.start()
    assert shutdown_started.wait(timeout=2)
    assert not shutdown_finished.is_set(), (
        "shutdown completed before the admitted request drained"
    )

    release.set()
    request_thread.join(timeout=2)
    shutdown_thread.join(timeout=2)

    assert result["status"] == 200
    assert result["body"]
    assert service.calls == 1
    assert not request_thread.is_alive()
    assert not shutdown_thread.is_alive()
    assert shutdown_errors == []
    assert not thread.is_alive()


@pytest.mark.flake_stress
def test_shutdown_closes_both_admission_gates_before_waiting_for_listener():
    events: list[str] = []

    class FakeServer:
        def __init__(self, name: str) -> None:
            self.name = name

        def stop_accepting(self) -> None:
            events.append(f"{self.name}:stop")

        def shutdown(self) -> None:
            events.append(f"{self.name}:shutdown")

        def server_close(self) -> None:
            events.append(f"{self.name}:close")

    class FakeThread:
        def __init__(self, name: str) -> None:
            self.name = name

        def join(self) -> None:
            events.append(f"{self.name}:join")

    service_main._shutdown_servers(
        (FakeServer("private"), FakeServer("action")),
        (FakeThread("private"), FakeThread("action")),
        started_count=2,
    )

    assert events[:2] == ["private:stop", "action:stop"]
    assert events[2:4] == ["private:shutdown", "action:shutdown"]
