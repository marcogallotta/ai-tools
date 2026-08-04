"""Unit contracts for the PGlite development-lane lifecycle harness."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.postgresql import pglite as harness


@dataclass
class _FakeProcess:
    exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


class _FakeResult:
    def fetchone(self):
        return (1, "PostgreSQL 17 PGlite")


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, statement: str):
        assert statement == "SELECT 1, version()"
        return _FakeResult()


def test_tcp_launcher_allows_rapid_protocol_reconnects() -> None:
    manager = object.__new__(harness.DishPGliteManager)
    manager.config = type(
        "Config",
        (),
        {"tcp_host": "127.0.0.1", "tcp_port": 55432},
    )()

    source = manager._generate_tcp_js_content("", "{}")

    assert f"maxConnections: {harness.PGLITE_MAX_CONNECTIONS}" in source
    assert source.index("await server.start()") < source.index("Server started on TCP")


def test_sql_readiness_requires_two_independent_protocol_connections(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_connect(dsn: str, **kwargs):
        calls.append({"dsn": dsn, **kwargs})
        return _FakeConnection()

    monkeypatch.setattr(harness.psycopg, "connect", fake_connect)
    manager = type("Manager", (), {"process": _FakeProcess()})()

    harness._verify_sql_readiness(manager, "host=127.0.0.1 port=55432")

    assert len(calls) == harness.PGLITE_READINESS_PROBES == 2
    assert all(call["autocommit"] is True for call in calls)
    assert all(call["prepare_threshold"] is None for call in calls)


def test_sql_readiness_rejects_process_that_died_after_tcp_accept() -> None:
    manager = type("Manager", (), {"process": _FakeProcess(exit_code=1)})()

    with pytest.raises(
        harness.PGliteLifecycleError,
        match="exited before SQL readiness probe 1",
    ):
        harness._verify_sql_readiness(manager, "unused")


def test_runtime_retries_only_failed_startup_with_fresh_manager(
    monkeypatch,
    tmp_path: Path,
) -> None:
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    managers: list[object] = []
    readiness_calls = 0

    class FakeManager:
        def __init__(self, config):
            self.config = config
            self.process = _FakeProcess()
            self.stopped = False
            managers.append(self)

        def start(self) -> None:
            return None

        def get_dsn(self) -> str:
            port = self.config["tcp_port"]
            return (
                f"host=127.0.0.1 port={port} dbname=postgres "
                "user=postgres password=postgres sslmode=disable"
            )

        def stop(self) -> None:
            self.stopped = True

    def fake_readiness(_manager, _dsn: str) -> None:
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            raise harness.PGliteLifecycleError("server closed the connection unexpectedly")

    ports = iter((55431, 55432))
    monkeypatch.setattr(harness, "NODE_MODULES", node_modules)
    monkeypatch.setattr(harness, "PGliteConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harness, "DishPGliteManager", FakeManager)
    monkeypatch.setattr(harness, "_verify_sql_readiness", fake_readiness)
    monkeypatch.setattr(harness, "_free_tcp_port", lambda: next(ports))

    with harness.pglite_runtime() as runtime:
        assert "port=55432" in runtime.libpq_dsn
        assert ":55432/" in runtime.sqlalchemy_url
        assert len(managers) == 2
        assert managers[0].stopped is True
        assert managers[1].stopped is False

    assert managers[1].stopped is True


def test_runtime_never_retries_exception_from_test_body(monkeypatch, tmp_path: Path) -> None:
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    managers: list[object] = []

    class FakeManager:
        def __init__(self, _config):
            self.process = _FakeProcess()
            self.stopped = False
            managers.append(self)

        def start(self) -> None:
            return None

        def get_dsn(self) -> str:
            return (
                "host=127.0.0.1 port=55432 dbname=postgres "
                "user=postgres password=postgres sslmode=disable"
            )

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(harness, "NODE_MODULES", node_modules)
    monkeypatch.setattr(harness, "PGliteConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harness, "DishPGliteManager", FakeManager)
    monkeypatch.setattr(harness, "_verify_sql_readiness", lambda *_args: None)
    monkeypatch.setattr(harness, "_free_tcp_port", lambda: 55432)

    with pytest.raises(AssertionError, match="real test failure"):
        with harness.pglite_runtime():
            raise AssertionError("real test failure")

    assert len(managers) == 1
    assert managers[0].stopped is True


def test_runtime_does_not_retry_non_lifecycle_startup_error(
    monkeypatch, tmp_path: Path
) -> None:
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    managers: list[object] = []

    class FakeManager:
        def __init__(self, _config):
            self.process = _FakeProcess()
            self.stopped = False
            managers.append(self)

        def start(self) -> None:
            raise ValueError("invalid launcher configuration")

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(harness, "NODE_MODULES", node_modules)
    monkeypatch.setattr(harness, "PGliteConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(harness, "DishPGliteManager", FakeManager)
    monkeypatch.setattr(harness, "_free_tcp_port", lambda: 55432)

    with pytest.raises(ValueError, match="invalid launcher configuration"):
        with harness.pglite_runtime():
            raise AssertionError("fixture must not yield")

    assert len(managers) == 1
    assert managers[0].stopped is True
