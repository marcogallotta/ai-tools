from __future__ import annotations

import json
from pathlib import Path

import pytest

from dish_pg import runtime_wiring_rehearsal as rehearsal


def _command_record(label: str, command: list[str], *, exit_status: int) -> dict:
    return {
        "label": label,
        "command": list(command),
        "completion_state": "completed",
        "termination_state": "none",
        "final_exit_status": exit_status,
        "timed_out": False,
        "failure": None,
        "log_path": f"/tmp/{label}.log",
        "output_sha256": "0" * 64,
    }


def test_exact_node_uses_postgresql_fixture_without_governed_lane_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, list[str]]] = []
    scenario = {
        "scenario": "runtime-wiring-section3",
        "completion_state": "scenario_assertions_completed",
        "evidence": {"service_health": {"identity": {"database": "dish_section3_test"}}},
    }
    monkeypatch.setattr(
        rehearsal, "_find_compose_command", lambda **_kwargs: (["docker", "compose"], [])
    )
    monkeypatch.setattr(rehearsal, "_probe_native", lambda _dsn: {"ok": True})

    def fake_run(command, **kwargs):
        commands.append((kwargs["label"], list(command)))
        return _command_record(kwargs["label"], list(command), exit_status=0)

    monkeypatch.setattr(rehearsal, "run_external_command", fake_run)
    monkeypatch.setattr(
        rehearsal,
        "_parse_junit",
        lambda _path: ({rehearsal.TEST_NODE: {"status": "passed"}}, []),
    )
    monkeypatch.setattr(rehearsal, "_load_single_scenario", lambda _path: (scenario, []))
    monkeypatch.setattr(rehearsal, "_read_json_files", lambda _path: [])
    monkeypatch.setattr(
        rehearsal,
        "_validate_evidence",
        lambda **_kwargs: {
            "ok": True,
            "errors": [],
            "process_count": 0,
            "distinct_pid_count": 0,
            "runtime_identity_report_count": 0,
        },
    )

    result = rehearsal.main(
        [
            "--output", str(tmp_path / "report.json"),
            "--evidence-dir", str(tmp_path / "evidence"),
            "--compose-project", "dish-section3-command-test",
        ]
    )

    assert result == 0
    pytest_command = next(
        command for label, command in commands
        if label == "pytest-section3-runtime-wiring-first-attempt"
    )
    assert "--postgresql" in pytest_command
    assert "--native-postgresql" not in pytest_command
    assert pytest_command[-1] == rehearsal.TEST_NODE


def test_cleanup_failure_overrides_blocked_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        rehearsal, "_find_compose_command", lambda **_kwargs: (["docker", "compose"], [])
    )
    monkeypatch.setattr(
        rehearsal,
        "run_external_command",
        lambda command, **kwargs: _command_record(
            kwargs["label"], list(command), exit_status=1
        ),
    )

    result = rehearsal.main(
        [
            "--output", str(output),
            "--evidence-dir", str(tmp_path / "evidence"),
            "--compose-project", "dish-section3-cleanup-test",
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 1
    assert report["status"] == "failed"
    assert report["manual_cleanup_required"] is True
    assert report["cleanup_errors"]
    assert report["manual_cleanup"]["command"][-3:] == [
        "down", "--volumes", "--remove-orphans"
    ]
