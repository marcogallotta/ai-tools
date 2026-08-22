"""Contracts for supervised one-node-per-process PGlite execution."""
from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
import json
import os
import select as select_module
import subprocess
import sys
import threading
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts" / "dish-pg-pglite"


def _load_runner():
    loader = SourceFileLoader("dish_pg_pglite_runner", str(RUNNER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def _result(
    runner,
    *,
    exit_code: int = 1,
    timed_out: bool = False,
    log_path: str = "pytest.log",
):
    return runner.CommandResult(
        command=["pytest"],
        exit_code=exit_code,
        duration_seconds=0.1,
        timed_out=timed_out,
        log_path=log_path,
        log_sha256="0" * 64,
        tracked_descendants=0,
        forced_cleanup=False,
        cleanup_remaining=[],
    )


def _write_junit(path: Path, *, failure: str | None = None) -> None:
    if failure is None:
        case = '<testcase classname="tests.example" name="test_case" />'
    else:
        case = (
            '<testcase classname="tests.example" name="test_case">'
            f'<failure message="failed">{failure}</failure></testcase>'
        )
    path.write_text(f'<testsuite tests="1">{case}</testsuite>', encoding="utf-8")


def _observe_descendant_snapshot(
    runner, monkeypatch
) -> tuple[threading.Event, list[int | None]]:
    observed = threading.Event()
    expected_pid: list[int | None] = [None]
    real_snapshot = runner.DescendantTracker._snapshot

    def observing_snapshot(tracker) -> None:
        real_snapshot(tracker)
        if expected_pid[0] in tracker._identities:
            observed.set()

    monkeypatch.setattr(runner.DescendantTracker, "_snapshot", observing_snapshot)
    return observed, expected_pid


def _read_ready_pid(fd: int, *, timeout: float = 2.0) -> int:
    readable, _, _ = select_module.select([fd], [], [], timeout)
    assert readable == [fd], "child readiness signal was not received"
    payload = os.read(fd, 64)
    assert payload, "child readiness signal was empty"
    return int(payload)


def test_classifier_does_not_call_any_pglite_assertion_infrastructure(tmp_path) -> None:
    runner = _load_runner()
    junit = tmp_path / "junit.xml"
    _write_junit(junit, failure="PGlite deterministic manifest assertion failed")

    status, _tests, assertions, infrastructure, _detail = runner._classify_node(
        result=_result(runner),
        junit_path=junit,
    )

    assert status == "failed"
    assert assertions == 1
    assert infrastructure == 0


def test_classifier_recognizes_connection_lifecycle_failure(tmp_path) -> None:
    runner = _load_runner()
    junit = tmp_path / "junit.xml"
    _write_junit(junit, failure="server closed the connection unexpectedly")

    status, _tests, assertions, infrastructure, _detail = runner._classify_node(
        result=_result(runner),
        junit_path=junit,
    )

    assert status == "infrastructure"
    assert assertions == 0
    assert infrastructure == 1


def test_classifier_uses_log_when_child_exits_before_junit(tmp_path) -> None:
    runner = _load_runner()
    log = tmp_path / "pytest.log"
    log.write_text("server closed the connection unexpectedly", encoding="utf-8")

    status, _tests, assertions, infrastructure, _detail = runner._classify_node(
        result=_result(runner, log_path=str(log)),
        junit_path=tmp_path / "missing.xml",
    )

    assert status == "infrastructure"
    assert assertions == 0
    assert infrastructure == 1


def test_optional_empty_quarantine_lane_passes(tmp_path) -> None:
    runner = _load_runner()

    result = runner._run_lane(
        python=sys.executable,
        selector="--quarantine",
        name="quarantine",
        artifact_root=tmp_path,
        node_timeout_seconds=1.0,
        collection_timeout_seconds=30.0,
        cleanup_grace_seconds=1.0,
        allow_empty=True,
    )

    assert result["inventory_count"] == 0
    assert result["nodes"] == []
    assert result["passed"] is True


def test_aggregate_junit_records_lifecycle_failures_as_errors(tmp_path) -> None:
    runner = _load_runner()
    output = tmp_path / "aggregate.xml"
    runner._write_aggregate_junit(
        lane_name="primary",
        node_results=[
            {
                "nodeid": "tests/example.py::test_case",
                "status": "infrastructure",
                "tests": 0,
                "assertion_failures": 0,
                "infrastructure_failures": 1,
                "detail": "server closed the connection unexpectedly",
                "duration_seconds": 0.1,
            }
        ],
        output=output,
    )

    root = runner.ET.parse(output).getroot()
    assert root.get("tests") == "1"
    assert root.get("failures") == "0"
    assert root.get("errors") == "1"
    assert root.find(".//error") is not None


def test_supervisor_times_out_and_kills_detached_descendant(
    tmp_path, monkeypatch
) -> None:
    runner = _load_runner()
    observed, expected_pid = _observe_descendant_snapshot(runner, monkeypatch)
    ready = tmp_path / "ready.fifo"
    release = tmp_path / "release.fifo"
    os.mkfifo(ready)
    os.mkfifo(release)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    source = tmp_path / "parent.py"
    source.write_text(
        """
import subprocess
import sys
import threading
child = subprocess.Popen([sys.executable, '-S', '-c', 'import threading; threading.Event().wait()'], start_new_session=True)
with open(sys.argv[1], 'w', encoding='utf-8') as ready:
    ready.write(str(child.pid))
    ready.flush()
with open(sys.argv[2], 'rb', buffering=0) as release:
    release.read(1)
""",
        encoding="utf-8",
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                runner._run_supervised,
                [sys.executable, "-S", str(source), str(ready), str(release)],
                log_path=tmp_path / "pytest.log",
                workspace=tmp_path / "work",
                timeout_seconds=0.75,
                cleanup_grace_seconds=0.5,
                env=os.environ.copy(),
            )
            child = psutil.Process(_read_ready_pid(ready_fd))
            expected_pid[0] = child.pid
            assert observed.wait(timeout=2.0)
            result = future.result(timeout=3.0)
    finally:
        os.close(release_fd)
        os.close(ready_fd)

    _, alive = psutil.wait_procs([child], timeout=3.0)
    assert alive == []
    assert result.timed_out is True
    assert result.cleanup_remaining == []


def test_supervisor_cleans_detached_descendant_after_parent_passes(
    tmp_path, monkeypatch
) -> None:
    runner = _load_runner()
    observed, expected_pid = _observe_descendant_snapshot(runner, monkeypatch)
    ready = tmp_path / "ready.fifo"
    release = tmp_path / "release.fifo"
    os.mkfifo(ready)
    os.mkfifo(release)
    ready_fd = os.open(ready, os.O_RDWR | os.O_NONBLOCK)
    release_fd = os.open(release, os.O_RDWR | os.O_NONBLOCK)
    source = tmp_path / "parent.py"
    source.write_text(
        """
import subprocess
import sys
import threading
child = subprocess.Popen([sys.executable, '-S', '-c', 'import threading; threading.Event().wait()'], start_new_session=True)
with open(sys.argv[1], 'w', encoding='utf-8') as ready:
    ready.write(str(child.pid))
    ready.flush()
with open(sys.argv[2], 'rb', buffering=0) as release:
    release.read(1)
""",
        encoding="utf-8",
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                runner._run_supervised,
                [sys.executable, "-S", str(source), str(ready), str(release)],
                log_path=tmp_path / "pytest.log",
                workspace=tmp_path / "work",
                timeout_seconds=5.0,
                cleanup_grace_seconds=0.5,
                env=os.environ.copy(),
            )
            child = psutil.Process(_read_ready_pid(ready_fd))
            expected_pid[0] = child.pid
            assert observed.wait(timeout=2.0)
            os.write(release_fd, b"1")
            result = future.result(timeout=3.0)
    finally:
        os.close(release_fd)
        os.close(ready_fd)

    _, alive = psutil.wait_procs([child], timeout=3.0)
    assert alive == []
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.forced_cleanup is True
    assert result.cleanup_remaining == []


def test_supervisor_captures_output_to_file_without_pipe_wait(tmp_path) -> None:
    runner = _load_runner()
    log = tmp_path / "pytest.log"

    result = runner._run_supervised(
        [sys.executable, "-c", "print('captured-output')"],
        log_path=log,
        workspace=tmp_path / "work",
        timeout_seconds=5.0,
        cleanup_grace_seconds=0.5,
        env=os.environ.copy(),
    )

    assert result.exit_code == 0
    assert log.read_text(encoding="utf-8").strip() == "captured-output"
    assert result.log_sha256 == runner._sha256_path(log)


def test_internal_governed_options_are_runner_only(tmp_path) -> None:
    inventory = tmp_path / "inventory.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pglite",
            "--collect-only",
            "-q",
            "--dish-internal-inventory-report",
            str(inventory),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "may be used only by repository lane scripts" in completed.stdout
    assert not inventory.exists()


def test_internal_inventory_uses_complete_collection(tmp_path) -> None:
    inventory = tmp_path / "inventory.json"
    env = os.environ.copy()
    env["DISH_INTERNAL_GOVERNED_RUNNER"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pglite",
            "--collect-only",
            "-q",
            "--dish-internal-inventory-report",
            str(inventory),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    assert payload["selector"] == "pglite"
    assert payload["nodeids"]
    assert len(payload["nodeids"]) == len(set(payload["nodeids"]))
    assert all(node.startswith("tests/postgresql/pglite/") for node in payload["nodeids"])


def test_manual_pglite_selector_with_explicit_path_remains_prohibited() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pglite",
            "--collect-only",
            "-q",
            "tests/postgresql/pglite/test_pglite_lane_contract.py",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode != 0
    assert "do not combine lane selectors with explicit test paths" in completed.stdout


def test_internal_exact_node_runs_after_full_inventory_collection(tmp_path) -> None:
    inventory = tmp_path / "inventory.json"
    exact_report = tmp_path / "exact.json"
    env = os.environ.copy()
    env["DISH_INTERNAL_GOVERNED_RUNNER"] = "1"
    first = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pglite",
            "--collect-only",
            "-q",
            "--dish-internal-inventory-report",
            str(inventory),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert first.returncode == 0, first.stdout
    nodeid = json.loads(inventory.read_text(encoding="utf-8"))["nodeids"][0]

    second = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pglite",
            "--collect-only",
            "-q",
            "--dish-internal-governed-node",
            nodeid,
            "--dish-internal-inventory-report",
            str(exact_report),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert second.returncode == 0, second.stdout
    payload = json.loads(exact_report.read_text(encoding="utf-8"))
    assert nodeid in payload["nodeids"]
    assert payload["selected_nodeids"] == [nodeid]


def test_pglite_readiness_requires_two_independent_sql_connections(monkeypatch) -> None:
    from tests.support.postgresql import pglite as support

    calls: list[str] = []

    class Cursor:
        def fetchone(self):
            return (1, "PostgreSQL 17 PGlite")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, _type, _value, _traceback):
            return None

        def execute(self, statement):
            assert statement == "SELECT 1, version()"
            return Cursor()

    def connect(dsn, *, connect_timeout):
        assert connect_timeout == 2
        calls.append(dsn)
        return Connection()

    monkeypatch.setattr(support.psycopg, "connect", connect)

    support._prove_sql_readiness("host=127.0.0.1 port=5432")

    assert calls == [
        "host=127.0.0.1 port=5432",
        "host=127.0.0.1 port=5432",
    ]


def test_pglite_socket_wrapper_allows_rapid_reconnects(tmp_path) -> None:
    from tests.support.postgresql import pglite as support

    config = support.PGliteConfig(
        work_dir=tmp_path,
        use_tcp=True,
        tcp_host="127.0.0.1",
        tcp_port=54321,
    )
    manager = support._DishPGliteManager(config)

    source = manager._generate_tcp_js_content("", "{}")

    assert "port: 54321," in source
    assert f"maxConnections: {support._PGLITE_MAX_CONNECTIONS}" in source


def test_ordinary_suite_collection_excludes_separate_pglite_inventory() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    collected = {
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip().startswith("tests/") and "::" in line
    }
    assert collected
    assert not any(node.startswith("tests/postgresql/pglite/") for node in collected)
