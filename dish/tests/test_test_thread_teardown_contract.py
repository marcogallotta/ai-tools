from __future__ import annotations

import ast
from pathlib import Path

from tests.support.ast_contracts import call_name
from tests.support.thread_teardown import join_thread, start_server_thread


TESTS_ROOT = Path(__file__).parent


class _RecordingServer:
    def __init__(self) -> None:
        self.poll_intervals: list[float] = []

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.poll_intervals.append(poll_interval)


def test_server_thread_preserves_server_default_poll_interval():
    server = _RecordingServer()
    thread = start_server_thread(server)

    join_thread(thread, timeout=1.0)

    assert server.poll_intervals == [0.5]


def test_server_thread_accepts_explicit_test_poll_interval():
    server = _RecordingServer()
    thread = start_server_thread(server, poll_interval=0.005)

    join_thread(thread, timeout=1.0)

    assert server.poll_intervals == [0.005]



def test_thread_joins_use_the_asserting_teardown_helper():
    violations = []
    for path in sorted(TESTS_ROOT.glob("*.py")):
        if path.name == "test_test_thread_teardown_contract.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr == "join"):
                continue
            has_timeout = any(keyword.arg == "timeout" for keyword in node.keywords)
            has_numeric_timeout = bool(
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, (int, float))
            )
            if has_timeout or has_numeric_timeout:
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == [], (
        "direct Thread.join calls can silently leave live threads; use "
        f"tests.support.thread_teardown.join_thread: {violations}"
    )


def test_test_threads_capture_uncaught_worker_exceptions():
    violations = []
    for path in sorted(TESTS_ROOT.glob("test_*.py")):
        if path.name in {"test_flake_tooling.py", "test_test_thread_teardown_contract.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "threading"
                and node.func.attr == "Thread"
            ):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == [], (
        "raw threading.Thread drops uncaught worker failures; construct threads "
        f"with managed_thread or start_thread: {violations}"
    )


def test_server_threads_use_the_managed_server_factory():
    violations = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path.name in {"thread_teardown.py", "test_flake_tooling.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if call_name(node) not in {"managed_thread", "start_thread"}:
                continue
            target = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "target"),
                node.args[0] if node.args else None,
            )
            if isinstance(target, ast.Attribute) and target.attr == "serve_forever":
                violations.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno}")
    assert violations == [], (
        "HTTP listeners must use start_server_thread so server lifecycle intent "
        f"is explicit: {violations}"
    )
