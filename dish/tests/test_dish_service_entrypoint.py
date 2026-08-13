from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from dish_service import __main__ as service_main
from dish_service.config import ServiceConfig


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.smoke
@pytest.mark.boundary
def test_dish_service_help_uses_the_repository_virtualenv_without_starting_service():
    completed = subprocess.run(
        [str(ROOT / "dish-service"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("usage: dish-service")
    assert "Run the single-process Dish HTTP service" in completed.stdout
    assert completed.stderr == ""


@pytest.mark.smoke
@pytest.mark.boundary
def test_dish_service_fails_closed_when_repository_virtualenv_is_missing(tmp_path):
    launcher = tmp_path / "dish-service"
    launcher.write_text((ROOT / "dish-service").read_text())
    launcher.chmod(0o755)
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)

    completed = subprocess.run(
        [str(launcher), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    expected = tmp_path / ".venv" / "bin" / "python"
    assert f"dish-service: no virtualenv at {expected}" in completed.stderr


@pytest.mark.smoke
def test_sigterm_handler_closes_admission_before_logging(monkeypatch):
    import threading

    stop = threading.Event()
    observed = []
    monkeypatch.setattr(
        service_main.LOG,
        "info",
        lambda *_args, **_kwargs: observed.append(stop.is_set()),
    )

    service_main._signal_handler(stop)(signal.SIGTERM, None)

    assert stop.is_set()
    assert observed == [True]


@pytest.mark.smoke
def test_second_listener_bind_failure_closes_first(monkeypatch):
    class PrivateServer:
        closed = False

        def server_close(self):
            self.closed = True

    private = PrivateServer()
    monkeypatch.setattr(service_main, "build_private_server", lambda _service: private)

    def fail_action(_service):
        raise OSError("address already in use")

    monkeypatch.setattr(service_main, "build_action_server", fail_action)
    with pytest.raises(OSError, match="address already in use"):
        service_main._build_servers(object())
    assert private.closed is True


@pytest.mark.smoke
def test_process_lock_contention_is_a_concise_startup_diagnostic(
    tmp_path, monkeypatch, caplog
):
    config = ServiceConfig(
        db_path=tmp_path / "shared.db",
        honest_root=tmp_path,
        agent_token="agent-token-123",
        admin_token="admin-token-456",
        action_token="action-token-789",
    )
    monkeypatch.setattr(service_main.ServiceConfig, "from_env", lambda: config)
    lock_path = config.db_path.with_suffix(config.db_path.suffix + ".service.lock")
    held = service_main.DatabaseProcessLock(
        lock_path, role="service", rule="service_process_lock_held"
    ).acquire()
    try:
        with caplog.at_level("ERROR", logger="dish.service"):
            result = service_main.main([])
    finally:
        held.release()

    assert result == 1
    assert "startup_failed" in caplog.text
    assert "service_process_lock_held" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.smoke
def test_listener_failure_stops_and_closes_both_servers():
    import threading

    class FakeServer:
        def __init__(self, *, failure=None):
            self.failure = failure
            self.stop = threading.Event()
            self.closed = False

        def serve_forever(self):
            if self.failure is not None:
                raise self.failure
            self.stop.wait(timeout=5)

        def shutdown(self):
            self.stop.set()

        def server_close(self):
            self.closed = True

    private = FakeServer()
    action = FakeServer(failure=RuntimeError("listener failed"))
    assert service_main._run_servers(private, action) == 1
    assert private.closed is True
    assert action.closed is True

@pytest.mark.smoke
def test_postgresql_startup_failure_exit_classification() -> None:
    from dish_tool.errors import DishRuleError
    from dish_tool.startup_exit import startup_exit_status

    stale = DishRuleError(
        "BACKEND_REJECTED",
        "stale schema",
        rule="postgresql_runtime_schema_mismatch",
        retryable=False,
    )
    unavailable = DishRuleError(
        "BACKEND_REJECTED",
        "database unavailable",
        rule="postgresql_authority_unavailable",
        retryable=True,
    )
    legacy = DishRuleError(
        "WRONG_STATE",
        "lock held",
        rule="service_process_lock_held",
        retryable=False,
    )

    assert startup_exit_status(stale) == 78
    assert startup_exit_status(unavailable) == 1
    assert startup_exit_status(legacy) == 1
