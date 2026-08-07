from __future__ import annotations

import json
from pathlib import Path

import pytest

from dish_pg import runtime_wiring_rehearsal as rehearsal
from tests.support.postgresql.runtime_wiring_evidence import valid_scenario_evidence


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
    identity = {"database": "dish_section3_runtime_wiring_test"}
    monkeypatch.setattr(
        rehearsal, "_find_compose_command", lambda **_kwargs: (["docker", "compose"], [])
    )
    monkeypatch.setattr(rehearsal, "_probe_native", lambda _dsn: {"ok": True})

    def fake_run(command, **kwargs):
        command = list(command)
        label = kwargs["label"]
        commands.append((label, command))
        if label == "pytest-section3-runtime-wiring-first-attempt":
            junit = Path(command[command.index("--junitxml") + 1])
            evidence = junit.parent
            junit.write_text(
                "<testsuite><testcase "
                'classname="tests.postgresql.native.test_runtime_wiring_rehearsal" '
                'name="test_runtime_wiring_rehearsal_across_service_and_worker_processes" '
                'time="0.1" /></testsuite>',
                encoding="utf-8",
            )
            scenarios = evidence / "scenarios"
            processes = evidence / "processes"
            identities = evidence / "runtime-identities"
            scenarios.mkdir()
            processes.mkdir()
            identities.mkdir()
            (scenarios / "scenario.json").write_text(
                json.dumps(
                    {
                        "scenario": "runtime-wiring-section3",
                        "completion_state": "scenario_assertions_completed",
                        "evidence": valid_scenario_evidence(identity),
                    }
                ),
                encoding="utf-8",
            )
            for pid, process_label in enumerate(
                sorted(rehearsal.REQUIRED_PROCESS_LABELS), start=1001
            ):
                (processes / f"{pid}.json").write_text(
                    json.dumps(
                        {
                            "label": process_label,
                            "pid": pid,
                            "command": ["python", "worker.py", process_label],
                            "completion_state": "completed",
                            "termination_state": "none",
                        }
                    ),
                    encoding="utf-8",
                )
            for role in sorted(rehearsal.REQUIRED_IDENTITY_ROLES):
                (identities / f"{role}.json").write_text(
                    json.dumps({"ok": True, "role": role, "identity": identity}),
                    encoding="utf-8",
                )
        return _command_record(label, command, exit_status=0)

    monkeypatch.setattr(rehearsal, "run_external_command", fake_run)

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
