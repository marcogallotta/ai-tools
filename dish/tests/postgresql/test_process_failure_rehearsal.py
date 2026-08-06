from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dish_pg import process_failure_rehearsal as rehearsal
from dish_pg import projection_worker
from tests.support.postgresql import process_failure as process_support
from tests.support.postgresql.certification import NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY

DISH_ROOT = Path(__file__).resolve().parents[2]


def _report_hash(payload: dict) -> str:
    copy = dict(payload)
    expected = copy.pop("report_sha256")
    actual = hashlib.sha256(rehearsal._canonical_bytes(copy)).hexdigest()
    assert actual == expected
    return expected


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


@pytest.mark.parametrize(
    "name",
    ["dish_section1_process_failure_test", "dish_custom_42_test"],
)
def test_process_failure_rehearsal_accepts_only_disposable_database_names(name: str) -> None:
    assert rehearsal._safe_database_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["dish", "dish_stage_a", "production_test", "dish_production_test", "dish_prod_test"],
)
def test_process_failure_rehearsal_rejects_unsafe_database_names(name: str) -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_database_name(name)


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_process_failure_rehearsal_rejects_nonfinite_or_nonpositive_timeouts(value: float) -> None:
    with pytest.raises(rehearsal.RehearsalConfigurationError):
        rehearsal._safe_timeout(value, name="test timeout")


def test_process_failure_rehearsal_emits_partial_scope_when_compose_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(rehearsal.shutil, "which", lambda _name: None)

    result = rehearsal.main(
        [
            "--output",
            str(output),
            "--evidence-dir",
            str(evidence),
            "--compose-project",
            "dish-section1-source-test",
        ]
    )

    assert result == 3
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert report["ok"] is False
    assert report["delivery_classification"] == "partial_worker_process_rehearsal"
    assert report["section1_implemented"] is False
    assert report["section1_certified"] is False
    assert report["worker_process_rehearsal_status"] == "not_run"
    assert report["worker_process_native_evidence_validated"] is False
    assert report["postgresql_identity"] is None
    assert report["test_inventory"] == list(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["test_summary"]["passed_count"] == 0
    assert report["test_summary"]["not_run_count"] == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert report["production_asana_touched"] is False
    statuses = {item["requirement"]: item["status"] for item in report["requirements"]}
    assert statuses["command_commit_before_response_and_exact_replay"] == "blocked"
    assert statuses["deadlock_and_serialization_policy"] == "not_exercised_no_defined_policy"
    assert "separate required implementation work" in rehearsal.COMMAND_PROCESS_BLOCKER
    _report_hash(report)


def test_process_failure_inventory_is_literal_process_owned_and_scenario_complete() -> None:
    assert len(rehearsal.PROCESS_TEST_INVENTORY) == len(set(rehearsal.PROCESS_TEST_INVENTORY))
    assert set(rehearsal.NODE_REQUIREMENTS) == set(rehearsal.PROCESS_TEST_INVENTORY)
    assert set(rehearsal.NODE_SCENARIOS) == set(rehearsal.PROCESS_TEST_INVENTORY)
    assert len(set(rehearsal.NODE_SCENARIOS.values())) == len(rehearsal.PROCESS_TEST_INVENTORY)
    assert all(
        node.startswith("tests/postgresql/native/test_process_failure_")
        for node in rehearsal.PROCESS_TEST_INVENTORY
    )
    assert all("::test_" in node for node in rehearsal.PROCESS_TEST_INVENTORY)
    assert set(rehearsal.PROCESS_TEST_INVENTORY).issubset(
        NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY
    )


def test_process_barriers_do_not_use_sleep_as_synchronization() -> None:
    paths = [
        DISH_ROOT / "tests/support/postgresql/process_failure.py",
        DISH_ROOT / "tests/support/postgresql/process_failure_adapter.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_projection.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_takeover.py",
        DISH_ROOT / "tests/postgresql/native/test_process_failure_disconnect.py",
    ]
    forbidden = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if "time.sleep(" in text or ".sleep(" in text:
            forbidden.append(str(path))
    assert forbidden == []


def test_database_credentials_are_redacted_from_persisted_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = (
        "postgresql+psycopg://secret-user:secret-password@127.0.0.1:5432/dish_test"
    )
    secret_values = ("secret-user", "secret-password")

    external_log = tmp_path / "external.log"
    external_record = tmp_path / "external.json"
    external = rehearsal.run_external_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", database_url],
        cwd=tmp_path,
        env=os.environ.copy(),
        log_path=external_log,
        timeout_seconds=10.0,
        termination_grace_seconds=1.0,
        label="credential-redaction",
        record_path=external_record,
    )
    assert external["final_exit_status"] == 0
    assert external["command"][-1] == (
        "postgresql+psycopg://<redacted>@127.0.0.1:5432/dish_test"
    )

    def fake_popen(command, **kwargs):
        assert command[-1] == database_url
        kwargs["stdout"].write(f"worker echoed {database_url}\n")
        kwargs["stdout"].flush()
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(process_support.subprocess, "Popen", fake_popen)
    command = [sys.executable, "worker.py", "--database-url", database_url]
    child = process_support._start_child(
        command,
        tmp_path=tmp_path,
        barrier=None,
        ledger=tmp_path / "ledger.json",
        scenario="credential-redaction",
        label="projection-redaction",
    )
    assert child.command == command
    child._close_log()

    process_record = json.loads(child.manifest_path.read_text(encoding="utf-8"))
    report_path = tmp_path / "report.json"
    rehearsal.write_json_atomic(
        report_path,
        {"external_command": external, "worker_process": process_record},
    )

    persisted = "\n".join(
        [
            external_record.read_text(encoding="utf-8"),
            external_log.read_text(encoding="utf-8"),
            child.manifest_path.read_text(encoding="utf-8"),
            child.log_path.read_text(encoding="utf-8"),
            report_path.read_text(encoding="utf-8"),
        ]
    )
    for secret in secret_values:
        assert secret not in persisted
    assert persisted.count("<redacted>") >= 4


def test_external_command_timeout_terminates_process_group_and_preserves_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "timeout.log"
    record_path = tmp_path / "timeout.json"
    script = (
        "import signal, subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()']); "
        "print('parent-ready', flush=True); signal.pause()"
    )
    signals: list[tuple[int, signal.Signals]] = []
    original_killpg = rehearsal.os.killpg

    def record_killpg(process_group_id: int, sent_signal: signal.Signals) -> None:
        signals.append((process_group_id, sent_signal))
        original_killpg(process_group_id, sent_signal)

    monkeypatch.setattr(rehearsal.os, "killpg", record_killpg)

    result = rehearsal.run_external_command(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ.copy(),
        log_path=log_path,
        timeout_seconds=3.0,
        termination_grace_seconds=0.5,
        label="timeout-test",
        record_path=record_path,
    )

    assert result["timed_out"] is True
    assert result["completion_state"] == "timed_out"
    assert result["termination_state"] in {"sigterm", "sigkill"}
    assert isinstance(result["final_exit_status"], int)
    assert "finite timeout" in str(result["failure"])
    assert "parent-ready" in log_path.read_text(encoding="utf-8")
    assert json.loads(record_path.read_text(encoding="utf-8")) == result
    assert signals
    assert signals[0] == (result["process_group_id"], signal.SIGTERM)


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
