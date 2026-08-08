from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _namespace():
    return runpy.run_path(str(ROOT / "scripts" / "dish-test-lane"))


def test_named_lanes_are_complete_and_obvious() -> None:
    lanes = _namespace()["LANES"]
    assert tuple(sorted(lanes)) == (
        "command-api-contracts",
        "experimental-parallel",
        "native-concurrency",
        "operational-certification",
        "pglite",
        "release-cutover",
        "schema-migrations",
    )
    assert all(phases for phases in lanes.values())
    assert all(phase.name.strip() and phase.command for phases in lanes.values() for phase in phases)


def test_lane_reuses_invoking_interpreter_instead_of_discovering_archive_venv() -> None:
    namespace = _namespace()
    assert namespace["PYTHON"] == sys.executable


def test_lane_stops_at_exact_failing_phase(monkeypatch) -> None:
    namespace = _namespace()
    calls: list[str] = []

    def fake_run(phase, *, env):
        calls.append(phase.name)
        return 7 if len(calls) == 2 else 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    assert main(["schema-migrations"]) == 7
    assert calls == [
        "focused schema and migration contracts",
        "SQLite database-boundary migration evidence",
    ]


def test_native_lane_reports_unavailable_before_running(monkeypatch, capsys) -> None:
    namespace = _namespace()
    phase = namespace["LANES"]["native-concurrency"][0]
    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: None)
    assert namespace["_run_phase"](phase, env={}) == 3
    assert "UNAVAILABLE [native PostgreSQL concurrency contracts]" in capsys.readouterr().err


def test_experimental_parallel_requires_workers_and_rejects_unreviewed_files() -> None:
    main = _namespace()["main"]
    with pytest.raises(SystemExit):
        main(["experimental-parallel"])
    with pytest.raises(SystemExit):
        main(
            [
                "experimental-parallel",
                "--workers",
                "2",
                "--test-file",
                "tests/test_lease_authority.py",
            ]
        )


def test_experimental_parallel_uses_optional_parallel_environment_and_exact_selection(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_parallel_preflight", lambda: 0)
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)

    assert main(
        [
            "experimental-parallel",
            "--workers",
            "4",
            "--test-file",
            "tests/test_commands.py",
        ]
    ) == 0
    command = commands[-1]
    assert command[:10] == (
        namespace["PARALLEL_PYTHON"],
        "-m",
        "pytest",
        "-p",
        "no:randomly",
        "-n",
        "4",
        "--dist",
        "loadfile",
        "-q",
    )
    assert command[10:] == ("tests/test_commands.py",)


def test_experimental_parallel_reports_missing_optional_environment(monkeypatch, capsys) -> None:
    namespace = _namespace()
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "PARALLEL_PYTHON", str(ROOT / ".missing-parallel" / "python"))
    monkeypatch.setitem(main.__globals__, "_run_phase", lambda *args, **kwargs: pytest.fail("ran"))

    assert main(["experimental-parallel", "--workers", "2"]) == 3
    assert "optional .venv-parallel is missing" in capsys.readouterr().err


def test_diagnostic_mode_changes_output_only_not_pytest_selection(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    expected_files = tuple(namespace["LANES"]["release-cutover"][0].command[4:])

    assert main(["release-cutover", "--diagnose"]) == 0
    command = commands[-1]
    assert "-q" not in command
    assert command[-2:] == ("-vv", "--durations=20")
    assert tuple(part for part in command if part.endswith(".py")) == expected_files
