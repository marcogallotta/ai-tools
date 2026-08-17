from __future__ import annotations

import os
from pathlib import Path
import signal
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pr_lifecycle_controller as c


def test_watcher_command_keeps_proven_v1_configuration():
    command = c._watcher_command()
    assert str(ROOT / "scripts" / "pr_lifecycle.py") in command
    assert command[command.index("--http-timeout") + 1] == "10"
    assert "--integration-authority" in command
    assert command[command.index("--local-integration-launcher") + 1] == str(
        ROOT / "tools" / "dish-local-integration-launcher"
    )
    assert command[-6:] == ["watch", "--dispatch", "--interval", "180", "--format", "table"]


@pytest.mark.parametrize(
    "error",
    [
        "pr_lifecycle: request timed out after 10s for https://api.github.com/x",
        "pr_lifecycle: request failed for https://app.asana.com/x: network unreachable",
        "pr_lifecycle: HTTP 429: rate limited",
        "pr_lifecycle: HTTP 503: unavailable",
        "pr_lifecycle: HTTP 403: API rate limit exceeded",
    ],
)
def test_transient_transport_errors_are_recoverable(error):
    assert c._transient(error)


def test_permission_or_semantic_failure_is_not_recoverable():
    assert not c._transient("pr_lifecycle: HTTP 403: Resource not accessible by integration")
    assert not c._transient("pr_lifecycle: integration authority is unavailable")


def test_backoff_is_bounded():
    assert [c._backoff(i) for i in (1, 2, 3, 4, 5, 6, 20)] == [1, 2, 4, 8, 16, 30, 30]


def test_start_refuses_unmanaged_existing_watcher(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    monkeypatch.setattr(c, "_active_watchers", lambda: [9090])
    with pytest.raises(c.ControllerError, match="refusing duplicate start"):
        c.start(paths)


def test_start_detaches_supervisor_and_reads_back(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    seen = {}

    class Process:
        pid = 4242

    def popen(command, **kwargs):
        seen.update(kwargs)
        return Process()

    monkeypatch.setattr(c, "_active_watchers", lambda: [])
    monkeypatch.setattr(c.subprocess, "Popen", popen)
    monkeypatch.setattr(c, "_ps", lambda pid: ("birth", "controller"))
    monkeypatch.setattr(c, "_snapshot", lambda paths: {
        "status": "running", "supervisor_live": True, "watcher_live": True,
        "supervisor_pid": 4242, "watcher_pid": 4243, "log_path": str(paths["log"]),
        "unmanaged_watchers": [],
    })
    assert c.start(paths) == 0
    assert seen["start_new_session"] is True
    assert seen["stdin"] is c.subprocess.DEVNULL
    assert seen["close_fds"] is True


def test_status_does_not_trust_pid_file(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    c._ensure(paths)
    c._atomic(paths["pid"], "5151\n")
    c._save(paths, run_id="run", status="running", supervisor_pid=5151, supervisor_birth="old")
    monkeypatch.setattr(c, "_alive", lambda pid: True)
    monkeypatch.setattr(c, "_ps", lambda pid: ("different", "python pr_lifecycle_controller.py run"))
    monkeypatch.setattr(c, "_active_watchers", lambda: [])
    assert c._snapshot(paths)["status"] == "stale"


def test_stop_refuses_unverified_live_pid(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    c._ensure(paths)
    c._atomic(paths["pid"], "6161\n")
    c._save(paths, run_id="run", status="running", supervisor_pid=6161, supervisor_birth="birth")
    monkeypatch.setattr(c, "_alive", lambda pid: True)
    monkeypatch.setattr(c, "_owned_supervisor", lambda pid, state: False)
    with pytest.raises(c.ControllerError, match="unverified PID"):
        c.stop(paths)


def test_owned_watcher_uses_birth_token_to_reject_pid_reuse(monkeypatch):
    state = {"watcher_pid": 7171, "watcher_birth": "old"}
    monkeypatch.setattr(c, "_alive", lambda pid: True)
    monkeypatch.setattr(c, "_ps", lambda pid: ("new", " ".join(c._watcher_command())))
    assert not c._owned_watcher(7171, state)



def test_supervisor_waits_for_parent_identity_binding(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    states = iter([
        {"run_id": "run", "supervisor_pid": None},
        {"run_id": "run", "supervisor_pid": 7777},
    ])
    pids = iter([None, 7777])
    sleeps = []
    monkeypatch.setattr(c, "_state", lambda paths: next(states))
    monkeypatch.setattr(c, "_pid", lambda paths: next(pids))
    monkeypatch.setattr(c.os, "getpid", lambda: 7777)
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    assert c._await_supervisor_binding(paths, "run") == {"run_id": "run", "supervisor_pid": 7777}
    assert sleeps == [0.02]

def test_transient_exit_restarts_then_nontransient_exit_fails(tmp_path, monkeypatch):
    paths = c._paths(tmp_path)
    c._ensure(paths)
    c._atomic(paths["pid"], f"{os.getpid()}\n")
    c._save(paths, run_id="run", status="starting", supervisor_pid=os.getpid())
    exits = iter([2, 2])
    sleeps = []

    class Child:
        def __init__(self):
            self.pid = 8000
        def wait(self):
            return next(exits)
        def poll(self):
            return 2
        def terminate(self):
            pass

    monkeypatch.setattr(c.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(c.signal, "signal", lambda *args, **kwargs: None)
    monkeypatch.setattr(c, "_ps", lambda pid: ("birth", "watcher"))
    errors = iter(["pr_lifecycle: HTTP 503: unavailable", "pr_lifecycle: bad configuration"])
    monkeypatch.setattr(c, "_tail_error", lambda path: next(errors))
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    assert c.supervise(paths, "run") == 2
    assert sleeps == [1]
    state = c._state(paths)
    assert state["status"] == "failed"
    assert state["last_error"] == "pr_lifecycle: bad configuration"
