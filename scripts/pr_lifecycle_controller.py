#!/usr/bin/env python3
"""Own the long-running local process for the PR lifecycle watcher."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path.home() / ".local" / "state" / "dish" / "pr-lifecycle"
WATCHER = ROOT / "scripts" / "pr_lifecycle.py"
INTEGRATION_LAUNCHER = ROOT / "tools" / "dish-local-integration-launcher"
INTERVAL = 180
HTTP_TIMEOUT = 10
MAX_BACKOFF = 30
CRASH_RESTART_LIMIT = 3


class ControllerError(RuntimeError):
    pass


def _paths(root: Path | None = None) -> dict[str, Path]:
    base = (root or Path(os.getenv("DISH_PR_LIFECYCLE_STATE_DIR", STATE_ROOT))).expanduser().resolve()
    return {
        "root": base,
        "pid": base / "controller.pid",
        "state": base / "controller.json",
        "lock": base / "controller.lock",
        "log": base / "controller.log",
        "projection": base / "lifecycle.json",
    }


def _ensure(paths: dict[str, Path]) -> None:
    paths["root"].mkdir(parents=True, exist_ok=True, mode=0o700)
    paths["root"].chmod(0o700)


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _state(paths: dict[str, Path]) -> dict[str, Any]:
    try:
        value = json.loads(paths["state"].read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot read {paths['state']}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"controller state is not a JSON object: {paths['state']}")
    return value


def _save(paths: dict[str, Path], **updates: Any) -> dict[str, Any]:
    value = _state(paths)
    value.update(updates)
    value["updated_at"] = time.time()
    _atomic(paths["state"], json.dumps(value, indent=2, sort_keys=True) + "\n")
    return value


def _pid(paths: dict[str, Path]) -> int | None:
    try:
        value = int(paths["pid"].read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ControllerError(f"invalid PID file {paths['pid']}: {exc}") from exc
    if value <= 1:
        raise ControllerError(f"unsafe PID {value} in {paths['pid']}")
    return value


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps(pid: int) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    line = result.stdout.strip()
    if not line:
        return "", ""
    # lstart is always five whitespace-delimited fields before args.
    fields = line.split(None, 5)
    return (" ".join(fields[:5]), fields[5]) if len(fields) == 6 else ("", "")


def _watcher_command(paths: dict[str, Path] | None = None) -> list[str]:
    projection = (paths or _paths())["projection"]
    return [
        sys.executable,
        str(WATCHER),
        "--repo", "marcogallotta/ai-tools",
        "--http-timeout", str(HTTP_TIMEOUT),
        "--projection-path", str(projection),
        "--integration-authority",
        "--local-integration-launcher", str(INTEGRATION_LAUNCHER),
        "watch", "--dispatch", "--interval", str(INTERVAL), "--format", "table",
    ]


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _conflicting_watcher_signature(command: str) -> bool:
    tokens = _command_tokens(command)
    script_index = next((
        index
        for index, token in enumerate(tokens)
        if token.replace("\\", "/") == "scripts/pr_lifecycle.py"
        or token.replace("\\", "/").endswith("/scripts/pr_lifecycle.py")
    ), None)
    if script_index is None:
        return False
    args = tokens[script_index + 1:]
    try:
        watch_index = args.index("watch")
    except ValueError:
        return False
    return "--dispatch" in args[watch_index + 1:]


def _owned_watcher_signature(command: str) -> bool:
    required = [str(WATCHER), "--integration-authority", str(INTEGRATION_LAUNCHER), "watch", "--dispatch"]
    return all(value in command for value in required) and f"--interval {INTERVAL}" in command


def _owned_supervisor(pid: int, state: dict[str, Any]) -> bool:
    if not _alive(pid) or pid != state.get("supervisor_pid") or not state.get("run_id"):
        return False
    birth, command = _ps(pid)
    return birth == state.get("supervisor_birth") and "pr_lifecycle_controller.py" in command and str(state["run_id"]) in command


def _owned_watcher(pid: int, state: dict[str, Any]) -> bool:
    if not _alive(pid) or pid != state.get("watcher_pid"):
        return False
    birth, command = _ps(pid)
    return birth == state.get("watcher_birth") and _owned_watcher_signature(command)


def _active_watchers() -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=", "-o", "args="], check=False, capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and _conflicting_watcher_signature(fields[1]):
            found.append(int(fields[0]))
    return sorted(set(found))


def _lock(paths: dict[str, Path]):
    class Lock:
        def __enter__(self):
            _ensure(paths)
            self.handle = paths["lock"].open("a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)

        def __exit__(self, *_: Any):
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
    return Lock()


def _tail_error(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    except OSError:
        return ""
    return next((line.strip() for line in reversed(lines) if "pr_lifecycle:" in line), "")


def _transient(error: str) -> bool:
    value = error.lower()
    if "request timed out" in value or "request failed for http" in value:
        return True
    match = re.search(r"http\s+(\d{3})", value)
    if not match:
        return False
    status = int(match.group(1))
    return status in {408, 425, 429} or 500 <= status <= 599 or (status == 403 and "rate limit" in value)


def _backoff(attempt: int) -> int:
    return min(MAX_BACKOFF, 2 ** max(0, attempt - 1))


def _snapshot(paths: dict[str, Path]) -> dict[str, Any]:
    state = _state(paths)
    supervisor = state.get("supervisor_pid")
    watcher = state.get("watcher_pid")
    supervisor_live = isinstance(supervisor, int) and _owned_supervisor(supervisor, state)
    watcher_live = isinstance(watcher, int) and _owned_watcher(watcher, state)
    unmanaged = [pid for pid in _active_watchers() if not watcher_live or pid != watcher]
    status = str(state.get("status") or "stopped")
    if supervisor_live and ((status == "running" and not watcher_live) or unmanaged):
        status = "degraded"
    elif not supervisor_live and (supervisor or _pid(paths)):
        status = "failed" if status == "failed" else "stale"
    elif not supervisor_live and unmanaged:
        status = "unmanaged-watcher"
    elif not supervisor_live:
        status = "stopped"
    return {
        **state, "status": status, "supervisor_live": supervisor_live, "watcher_live": watcher_live,
        "unmanaged_watchers": unmanaged, "log_path": str(paths["log"]), "projection_path": str(paths["projection"]),
    }


def _render(value: dict[str, Any]) -> str:
    fields = [f"controller: {value['status']}"]
    for key in ("supervisor_pid", "watcher_pid"):
        if value.get(key):
            fields.append(f"{key}={value[key]}")
    fields.append(f"log={value['log_path']}")
    if value.get("projection_path"):
        fields.append(f"projection={value['projection_path']}")
    if value.get("unmanaged_watchers"):
        fields.append("unmanaged_watchers=" + ",".join(map(str, value["unmanaged_watchers"])))
    if value.get("restart_count"):
        fields.append(f"restarts={value['restart_count']}")
    if value.get("last_error"):
        fields.append(f"last_error={value['last_error']}")
    return " | ".join(fields)


def _terminate(pid: int, verify, state: dict[str, Any], timeout: float = 5.0) -> None:
    if not verify(pid, state):
        raise ControllerError(f"refusing to signal unverified PID {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _alive(pid):
        if not verify(pid, state):
            raise ControllerError(f"PID {pid} changed identity while stopping")
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2.0
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _alive(pid):
            raise ControllerError(f"verified PID {pid} did not stop after SIGKILL")


def start(paths: dict[str, Path]) -> int:
    with _lock(paths):
        state = _state(paths)
        old_pid = _pid(paths)
        if old_pid and _owned_supervisor(old_pid, state):
            print(_render(_snapshot(paths)))
            return 1
        if old_pid and _alive(old_pid):
            raise ControllerError(f"PID file points to live unowned PID {old_pid}")
        old_watcher = state.get("watcher_pid")
        if isinstance(old_watcher, int) and _alive(old_watcher):
            if _owned_watcher(old_watcher, state):
                _terminate(old_watcher, _owned_watcher, state, 2.0)
            else:
                raise ControllerError(f"stale state points to live unowned watcher PID {old_watcher}")
        unmanaged = _active_watchers()
        if unmanaged:
            raise ControllerError(f"active unmanaged lifecycle watcher(s) {unmanaged}; refusing duplicate start")
        run_id = uuid.uuid4().hex
        _ensure(paths)
        paths["log"].touch(exist_ok=True, mode=0o600)
        paths["log"].chmod(0o600)
        _save(paths, schema="dish-pr-lifecycle-controller-state-v1", run_id=run_id, status="starting",
              supervisor_pid=None, watcher_pid=None, restart_count=0, last_error=None)
        command = [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(paths["root"]),
                   "_supervise", "--run-id", run_id]
        with paths["log"].open("ab", buffering=0) as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       cwd=str(ROOT), start_new_session=True, close_fds=True)
        birth, _ = _ps(process.pid)
        _atomic(paths["pid"], f"{process.pid}\n")
        _save(paths, supervisor_pid=process.pid, supervisor_birth=birth)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = _snapshot(paths)
        if value["supervisor_live"] and value["status"] in {"running", "reconnecting"}:
            print(_render(value))
            return 0
        if value["status"] == "failed" or not _alive(process.pid):
            raise ControllerError(_render(value))
        time.sleep(0.05)
    raise ControllerError("controller start did not reach running/reconnecting state")


def stop(paths: dict[str, Path]) -> int:
    with _lock(paths):
        state = _state(paths)
        pid = _pid(paths)
        if not pid or not _alive(pid):
            paths["pid"].unlink(missing_ok=True)
            _save(paths, status="stopped", supervisor_pid=None, watcher_pid=None)
            print(_render(_snapshot(paths)))
            return 0
        _save(paths, status="stopping")
        _terminate(pid, _owned_supervisor, state, 10.0)
        watcher = state.get("watcher_pid")
        if isinstance(watcher, int) and _alive(watcher):
            _terminate(watcher, _owned_watcher, state, 2.0)
        paths["pid"].unlink(missing_ok=True)
        _save(paths, status="stopped", supervisor_pid=None, watcher_pid=None)
    print(_render(_snapshot(paths)))
    return 0


def _await_supervisor_binding(paths: dict[str, Path], run_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        state = _state(paths)
        if (
            _pid(paths) == os.getpid()
            and state.get("run_id") == run_id
            and state.get("supervisor_pid") == os.getpid()
        ):
            return state
        if time.monotonic() >= deadline:
            raise ControllerError("supervisor identity does not match owned state")
        time.sleep(0.02)


def supervise(paths: dict[str, Path], run_id: str) -> int:
    _await_supervisor_binding(paths, run_id)
    stopping = False
    child: subprocess.Popen[Any] | None = None

    def on_stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    transient_attempt = crash_attempt = restarts = 0
    while not stopping:
        started = time.monotonic()
        try:
            child = subprocess.Popen(_watcher_command(paths), cwd=str(ROOT))
        except OSError as exc:
            crash_attempt += 1
            transient_attempt = 0
            if crash_attempt > CRASH_RESTART_LIMIT:
                _save(paths, status="failed", watcher_pid=None, last_error=f"watcher launch failed: {exc}")
                return 2
            restarts += 1
            delay = _backoff(crash_attempt)
            _save(paths, status="reconnecting", watcher_pid=None, restart_count=restarts,
                  next_retry_seconds=delay, last_error=f"watcher launch failed: {exc}")
            time.sleep(delay)
            continue
        birth, _ = _ps(child.pid)
        _save(paths, status="running", watcher_pid=child.pid, watcher_birth=birth,
              restart_count=restarts, next_retry_seconds=None, last_error=None)
        code = child.wait()
        child = None
        if stopping:
            break
        if time.monotonic() - started >= INTERVAL:
            transient_attempt = crash_attempt = 0
        error = _tail_error(paths["log"])
        if code == 2 and _transient(error):
            transient_attempt += 1
            crash_attempt = 0
        elif code == 2:
            _save(paths, status="failed", watcher_pid=None, last_error=error or "watcher exited with code 2")
            return 2
        else:
            crash_attempt += 1
            transient_attempt = 0
            if crash_attempt > CRASH_RESTART_LIMIT:
                _save(paths, status="failed", watcher_pid=None,
                      last_error=error or f"watcher repeatedly exited with code {code}")
                return code or 2
        restarts += 1
        delay = _backoff(transient_attempt or crash_attempt)
        _save(paths, status="reconnecting", watcher_pid=None, restart_count=restarts,
              next_retry_seconds=delay, last_error=error or f"watcher exited with code {code}")
        time.sleep(delay)
    _save(paths, status="stopped", watcher_pid=None)
    return 0


def logs(paths: dict[str, Path], lines: int, follow: bool) -> int:
    if lines < 0 or paths["log"].is_symlink():
        raise ControllerError("invalid log request")
    if not paths["log"].exists():
        raise ControllerError(f"log does not exist yet: {paths['log']}")
    content = paths["log"].read_text(encoding="utf-8", errors="replace").splitlines(True)
    sys.stdout.writelines(content[-lines:] if lines else [])
    if not follow:
        return 0
    with paths["log"].open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return 130


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dish-pr-lifecycle-controller")
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "restart", "status"):
        sub.add_parser(name)
    log = sub.add_parser("logs")
    log.add_argument("--lines", type=int, default=100)
    log.add_argument("--follow", action="store_true")
    internal = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    internal.add_argument("--run-id", required=True, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    paths = _paths(args.state_dir)
    try:
        if args.command == "start":
            return start(paths)
        if args.command == "stop":
            return stop(paths)
        if args.command == "restart":
            stop(paths)
            return start(paths)
        if args.command == "status":
            value = _snapshot(paths)
            print(_render(value))
            return 0 if value["status"] in {"running", "reconnecting"} else 1
        if args.command == "logs":
            return logs(paths, args.lines, args.follow)
        return supervise(paths, args.run_id)
    except ControllerError as exc:
        print(f"dish-pr-lifecycle-controller: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
