from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.support.dark_launch_acceptance import capture_report as _capture_report


pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dish-pg-dark-launch-test-acceptance"


def _namespace() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def _status_report() -> dict[str, Any]:
    checks = {
        name: {"status": "pass"}
        for name in (
            "private_cli_and_gpt_action_unchanged",
            "worker_credential_isolation",
            "active_epoch_external_effects_disabled",
            "shadow_origin_projection_rows_unclaimable",
        )
    }
    return {
        "preflight": {"status": "pass"},
        "capture": {"attempted": True, "first_attempt_status": "pass"},
        "worker_restart": {"attempted": True, "first_attempt_status": "pass"},
        "acceptance_checks": checks,
    }

def test_final_status_capture_failure_is_fail() -> None:
    final_status = _namespace()["_final_status"]
    report = _status_report()
    report["capture"] = {"attempted": True, "first_attempt_status": "fail"}

    assert final_status(report) == "fail"


def test_final_status_capture_pass_worker_blocked_is_partial() -> None:
    final_status = _namespace()["_final_status"]
    report = _status_report()
    report["worker_restart"] = {
        "attempted": False,
        "first_attempt_status": "blocked",
    }

    assert final_status(report) == "partial"


def test_final_status_worker_failure_is_fail() -> None:
    final_status = _namespace()["_final_status"]
    report = _status_report()
    report["worker_restart"] = {"attempted": True, "first_attempt_status": "fail"}

    assert final_status(report) == "fail"


def test_final_status_preflight_unavailable_is_blocked() -> None:
    final_status = _namespace()["_final_status"]
    report = _status_report()
    report["preflight"] = {"status": "blocked"}

    assert final_status(report) == "blocked"


def test_final_status_both_children_and_checks_pass() -> None:
    assert _namespace()["_final_status"](_status_report()) == "pass"

def _patch_successful_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, list[list[str]]]:
    namespace = _namespace()
    main = namespace["main"]
    calls: list[list[str]] = []
    capture_report = tmp_path / "capture-report.json"
    capture_report.write_text(json.dumps(_capture_report()), encoding="utf-8")

    def fake_run_first_attempt(**kwargs: Any) -> dict[str, Any]:
        command = list(kwargs["command"])
        calls.append(command)
        artifact_dir = Path(kwargs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout = artifact_dir / "stdout.txt"
        stderr = artifact_dir / "stderr.txt"
        if "host-capture" in command[1]:
            stdout.write_text("capture first attempt\n", encoding="utf-8")
        else:
            stdout.write_text(
                '  "run_marker": "20260806T120000Z-abc123",\n'
                f"scratch preserved at: {artifact_dir / 'scratch-preserved'}\n",
                encoding="utf-8",
            )
        stderr.write_text("", encoding="utf-8")
        return {
            "name": kwargs["name"],
            "first_attempt": True,
            "command": command,
            "started_at": "2026-08-06T12:00:00+00:00",
            "duration_seconds": 0.1,
            "exit_code": 0,
            "status": "pass",
            "stdout": {
                "path": str(stdout),
                "bytes": stdout.stat().st_size,
                "sha256": "a" * 64,
            },
            "stderr": {"path": str(stderr), "bytes": 0, "sha256": "b" * 64},
        }

    monkeypatch.setitem(
        main.__globals__,
        "_git_identity",
        lambda: {
            "git_commit": "c" * 40,
            "git_tree": "d" * 40,
            "tracked_tree_clean": True,
        },
    )
    monkeypatch.setitem(
        main.__globals__,
        "_preflight",
        lambda **_kwargs: (
            {
                "status": "pass",
                "service": {"unit": "dish-service-test.service"},
                "database": {"database": "dish_stage_a_dark_test"},
                "paths": {},
            },
            [],
        ),
    )
    monkeypatch.setitem(main.__globals__, "_run_first_attempt", fake_run_first_attempt)
    monkeypatch.setitem(
        main.__globals__,
        "_copy_capture_report",
        lambda _result, _artifact_dir: {
            "path": str(capture_report),
            "sha256": "e" * 64,
            "bytes": capture_report.stat().st_size,
            "source_path": "/test-state/report.json",
        },
    )
    monkeypatch.setitem(
        main.__globals__,
        "_post_worker_checks",
        lambda _url, _marker: {
            "active_epoch": {
                "status": "pass",
                "external_effects_enabled": False,
            },
            "shadow_projection_claimability": {
                "status": "pass",
                "row_count": 2,
                "rows_unchanged": True,
                "origin_filter_proof": {
                    "live_origin_selected": True,
                    "shadow_origin_selected": False,
                    "transaction_rolled_back": True,
                },
            },
        },
    )
    monkeypatch.setenv("DISH_PG_DATABASE_URL", "postgresql+psycopg://secret@test/db")
    return main, calls


def test_main_runs_each_child_once_and_writes_bounded_aggregate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    main, calls = _patch_successful_main(monkeypatch, tmp_path)
    output = tmp_path / "aggregate.json"

    assert main(
        [
            "--output",
            str(output),
            "--agent",
            "codex",
            "--source-archive-sha256",
            "f" * 64,
        ]
    ) == 0

    assert len(calls) == 2
    assert calls[0][1].endswith("dish-pg-host-capture-rehearsal")
    assert calls[1][1].endswith("dish-pg-certify-shadow-worker-restart")
    assert "postgresql+psycopg://secret@test/db" not in " ".join(calls[0] + calls[1])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["final_status"] == "pass"
    assert report["source_identity"]["received_archive_sha256"] == "f" * 64
    assert report["first_attempt_statuses"] == {
        "host_capture_rehearsal": "pass",
        "shadow_worker_restart_rehearsal": "pass",
    }
    assert set(report["child_report_hashes"]) == {
        "host_capture_rehearsal",
        "shadow_worker_restart_rehearsal",
    }
    serialized = output.read_text(encoding="utf-8")
    assert "postgresql+psycopg://secret@test/db" not in serialized
    assert len(serialized) < 20_000

def test_main_stops_after_failed_capture_first_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _namespace()
    main = namespace["main"]
    calls = 0

    def fake_run_first_attempt(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        artifact_dir = Path(kwargs["artifact_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stdout = artifact_dir / "stdout.txt"
        stderr = artifact_dir / "stderr.txt"
        stdout.write_text("failed\n", encoding="utf-8")
        stderr.write_text("failure\n", encoding="utf-8")
        return {
            "name": kwargs["name"],
            "first_attempt": True,
            "command": list(kwargs["command"]),
            "started_at": "2026-08-06T12:00:00+00:00",
            "duration_seconds": 0.1,
            "exit_code": -9,
            "status": "fail",
            "timeout": {"configured_seconds": 1.0, "timed_out": True},
            "process_group": {"started_new_session": True, "process_group_id": 1234},
            "cleanup": {
                "term_sent": True,
                "kill_sent": True,
                "process_exited": True,
                "final_returncode": -9,
            },
            "stdout": {"path": str(stdout), "bytes": 7, "sha256": "a" * 64},
            "stderr": {"path": str(stderr), "bytes": 8, "sha256": "b" * 64},
        }

    monkeypatch.setitem(
        main.__globals__,
        "_git_identity",
        lambda: {
            "git_commit": "c" * 40,
            "git_tree": "d" * 40,
            "tracked_tree_clean": True,
        },
    )
    monkeypatch.setitem(
        main.__globals__,
        "_preflight",
        lambda **_kwargs: ({"status": "pass"}, []),
    )
    monkeypatch.setitem(main.__globals__, "_run_first_attempt", fake_run_first_attempt)
    monkeypatch.setitem(main.__globals__, "_copy_capture_report", lambda *_args: None)
    monkeypatch.setenv("DISH_PG_DATABASE_URL", "postgresql+psycopg://test")
    output = tmp_path / "failed.json"

    assert main(["--output", str(output), "--agent", "codex"]) == 2

    report = json.loads(output.read_text(encoding="utf-8"))
    assert calls == 1
    assert report["final_status"] == "fail"
    assert report["capture"]["first_attempt_status"] == "fail"
    assert report["capture"]["timeout"]["timed_out"] is True
    assert report["capture"]["cleanup"]["kill_sent"] is True
    assert report["worker_restart"]["attempted"] is False
    assert len(report["preserved_failure_paths"]) == 2


def test_main_records_preflight_unavailability_as_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _namespace()
    main = namespace["main"]
    unavailable = namespace["PrerequisiteUnavailable"]
    monkeypatch.setitem(
        main.__globals__,
        "_git_identity",
        lambda: {
            "git_commit": "c" * 40,
            "git_tree": "d" * 40,
            "tracked_tree_clean": True,
        },
    )

    def unavailable_preflight(**_kwargs: Any) -> Any:
        raise unavailable("TEST service environment is unavailable")

    monkeypatch.setitem(main.__globals__, "_preflight", unavailable_preflight)
    monkeypatch.setenv("DISH_PG_DATABASE_URL", "postgresql+psycopg://test")
    output = tmp_path / "blocked.json"

    assert main(["--output", str(output), "--agent", "codex"]) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["final_status"] == "blocked"
    assert report["preflight"]["status"] == "blocked"
    assert report["failure_kind"] == "unavailable_prerequisite"
    assert report["child_commands"] == []


def test_main_records_capture_pass_worker_blocked_as_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    main, calls = _patch_successful_main(monkeypatch, tmp_path)
    unavailable = main.__globals__["PrerequisiteUnavailable"]

    def blocked_worker_environment(*_args: Any) -> Any:
        raise unavailable("worker prerequisite unavailable")

    monkeypatch.setitem(
        main.__globals__, "_sanitize_worker_environment", blocked_worker_environment
    )
    output = tmp_path / "partial.json"

    assert main(["--output", str(output), "--agent", "codex"]) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert report["final_status"] == "partial"
    assert report["capture"]["first_attempt_status"] == "pass"
    assert report["worker_restart"] == {
        "attempted": False,
        "first_attempt_status": "blocked",
    }


def test_child_timeout_preserves_process_group_evidence(tmp_path: Path) -> None:
    namespace = _namespace()
    child = tmp_path / "sleep.py"
    child.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    result = namespace["_run_first_attempt"](
        name="timeout-probe",
        command=[sys.executable, str(child)],
        cwd=tmp_path,
        environment=os.environ,
        artifact_dir=tmp_path / "artifacts",
        secrets=[],
        timeout_seconds=0.2,
        termination_grace_seconds=0.1,
    )

    assert result["status"] == "fail"
    assert result["timeout"]["timed_out"] is True
    assert result["process_group"]["started_new_session"] is True
    assert result["cleanup"]["term_sent"] is True
    assert result["cleanup"]["process_exited"] is True
    assert result["cleanup"]["process_group_exited"] is True
    assert result["duration_seconds"] < 3
    assert Path(result["stdout"]["path"]).is_file()
    assert Path(result["stderr"]["path"]).is_file()


def test_process_group_cleanup_escalates_after_sigterm_is_ignored(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    ready = tmp_path / "ready"
    child = tmp_path / "ignore-term.py"
    child.write_text(
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(child)],
        cwd=tmp_path,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()

    cleanup = namespace["_terminate_process_group"](process, grace_seconds=0.1)

    assert cleanup["term_sent"] is True
    assert cleanup["kill_sent"] is True
    assert cleanup["process_exited"] is True
    assert cleanup["process_group_exited"] is True
    assert cleanup["final_returncode"] == -signal.SIGKILL
