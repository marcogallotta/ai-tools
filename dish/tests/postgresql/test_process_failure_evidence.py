"""Artifact validation and worker lifecycle contracts for §1."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from dish_pg import process_failure_rehearsal as rehearsal
from dish_pg import projection_worker


def _scenario_record(nodeid: str, *, path: str = "scenario.json") -> dict:
    return {
        "path": path,
        "payload": {
            "format": "dish-section1-scenario-evidence-v2",
            "nodeid": nodeid,
            "scenario": rehearsal.NODE_SCENARIOS[nodeid],
            "completion_state": "scenario_assertions_completed",
            "evidence": {"boundary": "proven"},
        },
    }


def _process_record(nodeid: str, *, process_id: str = "process-1", path: str = "process.json") -> dict:
    return {
        "path": path,
        "payload": {
            "format": "dish-section1-process-record-v2",
            "process_id": process_id,
            "nodeid": nodeid,
            "final_exit_status": 0,
            "completion_state": "completed",
            "termination_state": "none",
        },
    }

def test_evidence_validation_accepts_one_scenario_and_final_process_per_passed_test() -> None:
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    validation = rehearsal._validate_evidence(
        cases={nodeid: {"status": "passed", "duration_seconds": 1.0, "detail": None}},
        junit_errors=[],
        scenario_records=[_scenario_record(nodeid)],
        process_records=[_process_record(nodeid)],
    )

    assert validation["ok"] is True
    assert validation["errors"] == []
    assert validation["valid_scenario_count"] == 1
    assert validation["valid_process_count"] == 1


@pytest.mark.parametrize(
    ("scenario_records", "process_records", "expected_error"),
    [
        ([], [_process_record(rehearsal.PROCESS_TEST_INVENTORY[0])], "exactly one valid scenario"),
        (
            [
                _scenario_record(rehearsal.PROCESS_TEST_INVENTORY[0], path="one.json"),
                _scenario_record(rehearsal.PROCESS_TEST_INVENTORY[0], path="two.json"),
            ],
            [_process_record(rehearsal.PROCESS_TEST_INVENTORY[0])],
            "duplicate scenario artifact",
        ),
        (
            [_scenario_record(rehearsal.PROCESS_TEST_INVENTORY[0])],
            [{"path": "broken.json", "read_error": "JSONDecodeError: broken"}],
            "process record unreadable",
        ),
        (
            [_scenario_record(rehearsal.PROCESS_TEST_INVENTORY[0])],
            [
                {
                    "path": "running.json",
                    "payload": {
                        "format": "dish-section1-process-record-v2",
                        "process_id": "running",
                        "nodeid": rehearsal.PROCESS_TEST_INVENTORY[0],
                        "final_exit_status": None,
                        "completion_state": "running",
                        "termination_state": "none",
                    },
                }
            ],
            "final integer exit status",
        ),
    ],
)
def test_evidence_validation_rejects_missing_duplicate_unreadable_or_incomplete_evidence(
    scenario_records: list[dict],
    process_records: list[dict],
    expected_error: str,
) -> None:
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    validation = rehearsal._validate_evidence(
        cases={nodeid: {"status": "passed", "duration_seconds": 1.0, "detail": None}},
        junit_errors=[],
        scenario_records=scenario_records,
        process_records=process_records,
    )

    assert validation["ok"] is False
    assert any(expected_error in error for error in validation["errors"])



def test_evidence_validation_rejects_process_state_inconsistent_with_junit_pass() -> None:
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    process = _process_record(nodeid)
    process["payload"].update(
        {
            "final_exit_status": -signal.SIGKILL,
            "completion_state": "timed_out",
            "termination_state": "sigkill",
        }
    )
    validation = rehearsal._validate_evidence(
        cases={nodeid: {"status": "passed", "duration_seconds": 1.0, "detail": None}},
        junit_errors=[],
        scenario_records=[_scenario_record(nodeid)],
        process_records=[process],
    )

    assert validation["ok"] is False
    assert any("timed-out process record" in error for error in validation["errors"])


def test_evidence_validation_rejects_failed_external_command_record() -> None:
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    validation = rehearsal._validate_evidence(
        cases={nodeid: {"status": "passed", "duration_seconds": 1.0, "detail": None}},
        junit_errors=[],
        scenario_records=[_scenario_record(nodeid)],
        process_records=[_process_record(nodeid)],
        command_records=[
            {
                "path": "compose-stop.json",
                "payload": {
                    "format": "dish-external-command-record-v1",
                    "completion_state": "timed_out",
                    "final_exit_status": -signal.SIGKILL,
                    "timed_out": True,
                },
            }
        ],
    )

    assert validation["ok"] is False
    assert any("external command record reports failure" in error for error in validation["errors"])

def test_evidence_validation_distinguishes_not_run_from_passed() -> None:
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    validation = rehearsal._validate_evidence(
        cases={},
        junit_errors=[],
        scenario_records=[_scenario_record(nodeid)],
        process_records=[_process_record(nodeid)],
    )

    assert validation["ok"] is False
    assert any("not_run test" in error for error in validation["errors"])
    summary = rehearsal._test_summary({})
    assert summary["passed_count"] == 0
    assert summary["not_run_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)


def test_runner_terminates_and_finalizes_worker_left_running_by_pytest(tmp_path: Path) -> None:
    processes = tmp_path / "processes"
    processes.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()"],
        start_new_session=True,
    )
    nodeid = rehearsal.PROCESS_TEST_INVENTORY[0]
    manifest = processes / "orphan.json"
    rehearsal.write_json_atomic(
        manifest,
        {
            "format": "dish-section1-process-record-v2",
            "process_id": "orphan",
            "nodeid": nodeid,
            "pid": process.pid,
            "process_group_id": process.pid,
            "started_at": "2026-08-06T00:00:00+00:00",
            "completed_at": None,
            "final_exit_status": None,
            "completion_state": "running",
            "termination_state": "none",
        },
    )

    errors = rehearsal._terminate_incomplete_process_groups(
        processes, timeout_seconds=3.0
    )
    exit_status = process.wait(timeout=3.0)

    assert errors == []
    assert exit_status == -signal.SIGKILL
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["final_exit_status"] == -signal.SIGKILL
    assert payload["completion_state"] == "terminated"
    assert payload["termination_state"] == "sigkill"
    assert payload["completed_at"] is not None


def test_projection_worker_once_uses_one_claim_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeEngine:
        def dispose(self) -> None:
            calls.append("dispose")

    class FakeWorker:
        def __init__(self, **_kwargs) -> None:
            pass

        def run_once(self) -> bool:
            calls.append("once")
            return False

        def run_forever(self) -> None:
            calls.append("forever")

        def request_shutdown(self) -> None:
            calls.append("shutdown")

    monkeypatch.setattr(projection_worker, "create_database_engine", lambda _settings: FakeEngine())
    monkeypatch.setattr(projection_worker, "session_factory", lambda _engine: object())
    monkeypatch.setattr(projection_worker, "load_adapter", lambda _path: object())
    monkeypatch.setattr(projection_worker, "ProjectionWorker", FakeWorker)
    monkeypatch.setattr(projection_worker.signal, "signal", lambda *_args: None)

    result = projection_worker.main(
        [
            "--database-url",
            "postgresql+psycopg://dish:dish@127.0.0.1:55442/dish_section1_test",
            "--worker-id",
            "one-shot",
            "--adapter",
            "module:adapter",
            "--once",
        ]
    )

    assert result == 0
    assert calls == ["once", "dispose"]
