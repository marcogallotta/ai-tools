"""Process and control-point helpers for the Section 4 local rehearsal.

The helpers reuse the maintained PostgreSQL TEST mode of ``dish-service`` over
its existing loopback HTTP transport. Workers and the service use explicit
Unix-socket barriers for deterministic fault injection; no timing sleep decides
when a fault may be injected.
"""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

BARRIER_EVENT_SCHEMA = "dish-section4-barrier-event-v1"


class RuntimeControlError(RuntimeError):
    """A child process or explicit control point did not satisfy its contract."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _redact_arg(value: object) -> str:
    text = str(value)
    if "postgresql://" in text or "postgresql+psycopg://" in text:
        prefix, _, tail = text.partition("//")
        if "@" in tail:
            return prefix + "//<redacted>@" + tail.split("@", 1)[1]
    return text


def _write_json_line(connection: socket.socket, value: Mapping[str, Any]) -> None:
    connection.sendall(_canonical_bytes(value) + b"\n")


def _read_json_line(connection: socket.socket, *, limit: int = 1024 * 1024) -> dict[str, Any]:
    received = bytearray()
    while not received.endswith(b"\n"):
        chunk = connection.recv(65536)
        if not chunk:
            raise RuntimeControlError("control socket closed before one complete JSON line")
        received.extend(chunk)
        if len(received) > limit:
            raise RuntimeControlError("control message exceeded the one-megabyte safety limit")
    try:
        value = json.loads(received.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeControlError("control message was not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeControlError("control message must be a JSON object")
    return value


@dataclass
class BarrierEvent:
    label: str
    payload: Mapping[str, Any]
    pid: int
    _connection: socket.socket = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    def release(self) -> None:
        if self._released:
            return
        _write_json_line(
            self._connection,
            {"schema": BARRIER_EVENT_SCHEMA, "action": "continue", "label": self.label},
        )
        self._connection.close()
        self._released = True

    def close_without_release(self) -> None:
        if self._released:
            return
        self._connection.close()
        self._released = True


class BarrierServer:
    """One-shot explicit process barrier over an owned Unix socket."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._socket: socket.socket | None = None

    def __enter__(self) -> "BarrierServer":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        server.listen(1)
        self._socket = server
        return self

    def wait(self, label: str, *, timeout_seconds: float) -> BarrierEvent:
        if self._socket is None:
            raise RuntimeControlError("barrier server is not active")
        self._socket.settimeout(timeout_seconds)
        try:
            connection, _ = self._socket.accept()
        except TimeoutError as exc:
            raise RuntimeControlError(f"barrier {label!r} was not reached") from exc
        connection.settimeout(timeout_seconds)
        try:
            value = _read_json_line(connection)
        except Exception:
            connection.close()
            raise
        if value.get("schema") != BARRIER_EVENT_SCHEMA:
            connection.close()
            raise RuntimeControlError("barrier event schema mismatch")
        if value.get("label") != label:
            connection.close()
            raise RuntimeControlError(
                f"expected barrier {label!r}, received {value.get('label')!r}"
            )
        try:
            pid = int(value["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            connection.close()
            raise RuntimeControlError("barrier event omitted a valid PID") from exc
        payload = value.get("payload")
        if not isinstance(payload, dict):
            connection.close()
            raise RuntimeControlError("barrier payload must be an object")
        return BarrierEvent(label=label, payload=payload, pid=pid, _connection=connection)

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.path.unlink(missing_ok=True)


def reach_barrier(path: Path, label: str, payload: Mapping[str, Any] | None = None) -> None:
    """Child-side barrier implementation shared by local adapters and fixtures."""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path.expanduser().resolve()))
        _write_json_line(
            client,
            {
                "schema": BARRIER_EVENT_SCHEMA,
                "label": label,
                "pid": os.getpid(),
                "payload": dict(payload or {}),
            },
        )
        response = _read_json_line(client)
    expected = {"schema": BARRIER_EVENT_SCHEMA, "action": "continue", "label": label}
    if response != expected:
        raise RuntimeControlError(f"barrier {label!r} received invalid release {response!r}")


@dataclass
class ManagedChild:
    label: str
    argv: list[str]
    process: subprocess.Popen[str]
    log_path: Path
    started_at: str
    ready: Mapping[str, Any] | None = None
    intentional_kill: bool = False
    stop_record: Mapping[str, Any] | None = None
    _log_handle: Any = field(default=None, repr=False)
    _pump_thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def process_group_id(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @classmethod
    def spawn(
        cls,
        *,
        label: str,
        argv: Sequence[str | Path],
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
        ready_schema: str | None = None,
        ready_timeout_seconds: float = 30.0,
    ) -> "ManagedChild":
        log_path = log_path.expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8")
        os.chmod(log_path, 0o600)
        safe_argv = [str(item) for item in argv]
        process = subprocess.Popen(
            safe_argv,
            cwd=cwd,
            env=dict(env),
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if ready_schema else handle,
            stderr=handle,
            start_new_session=True,
            bufsize=1,
        )
        child = cls(
            label=label,
            argv=[_redact_arg(item) for item in safe_argv],
            process=process,
            log_path=log_path,
            started_at=datetime.now(timezone.utc).isoformat(),
            _log_handle=handle,
        )
        if ready_schema is not None:
            child.ready = child._read_ready(ready_schema, ready_timeout_seconds)
            child._start_stdout_pump()
        return child

    def _read_ready(self, schema: str, timeout_seconds: float) -> Mapping[str, Any]:
        if self.process.stdout is None:
            raise RuntimeControlError(f"{self.label} has no readiness channel")
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            events = selector.select(timeout_seconds)
        finally:
            selector.close()
        if not events:
            self.terminate(grace_seconds=2.0, force=True)
            raise RuntimeControlError(f"{self.label} did not publish readiness")
        line = self.process.stdout.readline()
        if not line:
            code = self.process.poll()
            self.terminate(grace_seconds=2.0, force=True)
            raise RuntimeControlError(
                f"{self.label} exited before readiness (exit={code}, log={self.log_path})"
            )
        self._log_handle.write(line)
        self._log_handle.flush()
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            self.terminate(grace_seconds=2.0, force=True)
            raise RuntimeControlError(f"{self.label} readiness was not JSON") from exc
        if not isinstance(value, dict) or value.get("schema") != schema:
            self.terminate(grace_seconds=2.0, force=True)
            raise RuntimeControlError(f"{self.label} readiness schema mismatch")
        if int(value.get("pid", -1)) != self.process.pid:
            self.terminate(grace_seconds=2.0, force=True)
            raise RuntimeControlError(f"{self.label} readiness PID mismatch")
        return value

    def _start_stdout_pump(self) -> None:
        assert self.process.stdout is not None

        def pump() -> None:
            try:
                for line in self.process.stdout:
                    self._log_handle.write(line)
                    self._log_handle.flush()
            finally:
                self.process.stdout.close()

        thread = threading.Thread(target=pump, name=f"{self.label}-stdout", daemon=True)
        thread.start()
        self._pump_thread = thread

    def wait(self, *, timeout_seconds: float, check: bool = True) -> int:
        try:
            code = self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeControlError(
                f"{self.label} did not exit within {timeout_seconds:g}s"
            ) from exc
        self._finalize_log()
        if check and code != 0:
            raise RuntimeControlError(
                f"{self.label} failed with exit {code}; log={self.log_path}"
            )
        return code

    def terminate(self, *, grace_seconds: float, force: bool = False) -> Mapping[str, Any]:
        started = time.monotonic()
        signals: list[str] = []
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL if force else signal.SIGTERM)
                signals.append("SIGKILL" if force else "SIGTERM")
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    signals.append("SIGKILL")
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    pass
        stopped = self.process.poll() is not None
        self._finalize_log()
        record = {
            "label": self.label,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "stopped": stopped,
            "returncode": self.process.poll(),
            "signals": signals,
            "duration_seconds": time.monotonic() - started,
            "log_path": str(self.log_path),
            "cleanup_commands": [
                f"kill -TERM -- -{self.process_group_id}",
                f"kill -KILL -- -{self.process_group_id}",
            ],
        }
        self.stop_record = record
        return record

    def kill_for_fault(self, *, grace_seconds: float = 5.0) -> Mapping[str, Any]:
        self.intentional_kill = True
        return self.terminate(grace_seconds=grace_seconds, force=True)

    def _finalize_log(self) -> None:
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
        if self._log_handle is not None and not self._log_handle.closed:
            self._log_handle.flush()
            os.fsync(self._log_handle.fileno())
            self._log_handle.close()

    def evidence(self) -> dict[str, Any]:
        digest = None
        if self.log_path.is_file():
            digest = hashlib.sha256(self.log_path.read_bytes()).hexdigest()
        return {
            "label": self.label,
            "argv": self.argv,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "started_at": self.started_at,
            "ready": self.ready,
            "running": self.running,
            "returncode": self.process.poll(),
            "intentional_kill": self.intentional_kill,
            "log_path": str(self.log_path),
            "log_sha256": digest,
            "stop_record": self.stop_record,
        }


class PendingRuntimeRequest:
    def __init__(self, target, *args, **kwargs) -> None:
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None

        def invoke() -> None:
            try:
                self.result = target(*args, **kwargs)
            except BaseException as exc:  # explicit thread boundary
                self.error = exc

        self._thread = threading.Thread(target=invoke, daemon=True)
        self._thread.start()

    def finish(self, *, timeout_seconds: float, allow_error: bool = False) -> dict[str, Any] | None:
        self._thread.join(timeout_seconds)
        if self._thread.is_alive():
            raise RuntimeControlError("runtime request did not finish after its barrier was released")
        if self.error is not None and not allow_error:
            raise RuntimeControlError(f"runtime request failed: {type(self.error).__name__}: {self.error}")
        return self.result


class ServiceRuntimeClient:
    """Controller for the maintained ``dish-service --postgresql-test-runtime`` path."""

    def __init__(
        self,
        *,
        entry_point: Path,
        database_url: str,
        expected_database: str,
        expected_schema_head: str,
        expected_release: str,
        generation_id: str,
        owner_id: str,
        run_id: str,
        evidence_dir: Path,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
        python_executable: str,
    ) -> None:
        self.entry_point = entry_point
        self.database_url = database_url
        self.expected_database = expected_database
        self.expected_schema_head = expected_schema_head
        self.expected_release = expected_release
        self.generation_id = generation_id
        self.owner_id = owner_id
        self.run_id = run_id
        self.evidence_dir = evidence_dir
        self.cwd = cwd
        self.env = dict(env)
        self.log_path = log_path
        self.python_executable = python_executable
        self.child: ManagedChild | None = None
        self.children: list[ManagedChild] = []
        self._restart_number = 0
        self.private_port = self._free_port()
        self.action_port = self._free_port(exclude={self.private_port})
        self.agent_token = "section4-agent-token"
        self.admin_token = "section4-admin-token"
        self.action_token = "section4-action-token"

    @staticmethod
    def _free_port(*, exclude: set[int] | None = None) -> int:
        excluded = exclude or set()
        while True:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.bind(("127.0.0.1", 0))
                port = int(server.getsockname()[1])
            if port not in excluded:
                return port

    @property
    def private_base_url(self) -> str:
        return f"http://127.0.0.1:{self.private_port}"

    def _http_json(
        self,
        url: str,
        *,
        token: str | None = None,
        body: Mapping[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> tuple[int, dict[str, Any]]:
        data = None if body is None else _canonical_bytes(dict(body))
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return int(response.status), json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            return int(exc.code), payload

    def _wait_health(self, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: object = None
        while time.monotonic() < deadline:
            if self.child is not None and self.child.process.poll() is not None:
                raise RuntimeControlError(
                    f"PostgreSQL TEST service exited before readiness; log={self.child.log_path}"
                )
            try:
                status, payload = self._http_json(
                    f"{self.private_base_url}/health", timeout_seconds=2.0
                )
                last = (status, payload)
                if status == 200 and payload.get("ok") is True:
                    return payload
            except (OSError, ValueError) as exc:
                last = exc
            time.sleep(0.05)
        raise RuntimeControlError(f"PostgreSQL TEST service health timed out: {last!r}")

    def start(
        self,
        *,
        control_point: str | None = None,
        request_id: str | None = None,
        barrier_socket: Path | None = None,
    ) -> Mapping[str, Any]:
        if self.child is not None and self.child.running:
            raise RuntimeControlError("service runtime is already running")
        if control_point is not None and (request_id is None or barrier_socket is None):
            raise RuntimeControlError("service control point requires request identity and barrier")
        self._restart_number += 1
        argv: list[str | Path] = []
        if self.entry_point.suffix == ".py":
            argv.append(self.python_executable)
        argv.extend(
            [
                self.entry_point,
                "--postgresql-test-runtime",
                "--database-url",
                self.database_url,
                "--expected-database",
                self.expected_database,
                "--expected-schema-head",
                self.expected_schema_head,
                "--expected-release",
                self.expected_release,
                "--expected-generation-id",
                self.generation_id,
                "--cursor-secret",
                "section4-runtime-cursor-secret-32-bytes",
                "--state-dir",
                self.evidence_dir / f"service-state-{self._restart_number}",
            ]
        )
        child_env = dict(self.env)
        child_env.update(
            {
                "DISH_PROFILE": "test",
                "DISH_SERVICE_BIND": "127.0.0.1",
                "DISH_SERVICE_PORT": str(self.private_port),
                "DISH_ACTION_BIND": "127.0.0.1",
                "DISH_ACTION_PORT": str(self.action_port),
                "DISH_SERVICE_AGENT_TOKEN": self.agent_token,
                "DISH_SERVICE_ADMIN_TOKEN": self.admin_token,
                "DISH_SERVICE_ACTION_TOKEN": self.action_token,
            }
        )
        for name in (
            "DISH_SECTION4_SERVICE_CONTROL_POINT",
            "DISH_SECTION4_SERVICE_REQUEST_ID",
            "DISH_SECTION4_SERVICE_BARRIER_SOCKET",
        ):
            child_env.pop(name, None)
        if control_point is not None:
            child_env.update(
                {
                    "DISH_SECTION4_SERVICE_CONTROL_POINT": control_point,
                    "DISH_SECTION4_SERVICE_REQUEST_ID": str(request_id),
                    "DISH_SECTION4_SERVICE_BARRIER_SOCKET": str(barrier_socket),
                }
            )
        log_path = self.log_path.with_name(
            f"{self.log_path.stem}-{self._restart_number}{self.log_path.suffix}"
        )
        child = ManagedChild.spawn(
            label=f"section4-postgresql-test-service-{self._restart_number}",
            argv=argv,
            cwd=self.cwd,
            env=child_env,
            log_path=log_path,
        )
        self.child = child
        self.children.append(child)
        health = self._wait_health()
        return {
            "health": health,
            "transport": "dish-service-http",
            "private_base_url": self.private_base_url,
            "process": child.evidence(),
        }

    def request(self, payload: Mapping[str, Any], *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        action = payload.get("action")
        if action == "health":
            _status, health = self._http_json(
                f"{self.private_base_url}/health", timeout_seconds=timeout_seconds
            )
            return health
        if action != "command":
            raise RuntimeControlError(f"unsupported service request action: {action!r}")
        command = str(payload["command"])
        request_id = payload.get("command_request_id")
        client: dict[str, Any] = {"run_id": self.run_id}
        if request_id is not None:
            client["request_id"] = str(request_id)
        status, response = self._http_json(
            f"{self.private_base_url}/v1/commands/{command}",
            token=self.agent_token,
            body={"client": client, "arguments": dict(payload.get("arguments") or {})},
            timeout_seconds=timeout_seconds,
        )
        if status != 200 or response.get("ok") is not True:
            return {"ok": False, "http_status": status, "error": response}
        result = response.get("data")
        if not isinstance(result, dict):
            raise RuntimeControlError("service command response omitted data object")
        return {"ok": True, "result": result}

    def pending_request(self, payload: Mapping[str, Any]) -> PendingRuntimeRequest:
        control_point = payload.get("control_point")
        if control_point is not None:
            self.stop()
            self.start(
                control_point=str(control_point),
                request_id=str(payload.get("command_request_id")),
                barrier_socket=Path(str(payload.get("barrier_socket"))),
            )
        return PendingRuntimeRequest(self.request, payload, timeout_seconds=120.0)

    def command(
        self,
        *,
        command: str,
        arguments: Mapping[str, Any],
        request_id: str | None,
        control_point: str | None = None,
        barrier_socket: Path | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "command",
            "command": command,
            "arguments": dict(arguments),
            "owner_id": self.owner_id,
            "run_id": self.run_id,
        }
        if request_id is not None:
            payload["command_request_id"] = request_id
        if control_point is not None:
            if barrier_socket is None:
                raise RuntimeControlError("command control point requires a barrier socket")
            payload["control_point"] = control_point
            payload["barrier_socket"] = str(barrier_socket)
        response = self.request(payload, timeout_seconds=120.0)
        if not response.get("ok"):
            raise RuntimeControlError(
                f"service command {command!r} failed: {response.get('error')}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeControlError("service command response omitted result object")
        return result

    def stop(self, *, grace_seconds: float = 5.0) -> Mapping[str, Any]:
        if self.child is None or not self.child.running:
            return {"stopped": True, "reason": "not_started"}
        return self.child.terminate(grace_seconds=grace_seconds)

    def kill_for_fault(self) -> Mapping[str, Any]:
        if self.child is None:
            raise RuntimeControlError("service runtime is not running")
        return self.child.kill_for_fault()
