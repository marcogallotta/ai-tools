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
        "native-concurrency",
        "operational-certification",
        "parallel-safe",
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


def test_parallel_safe_can_run_serially_and_rejects_unreviewed_files(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)
    assert main(["parallel-safe", "--test-file", "tests/test_commands.py"]) == 0
    assert "-n" not in commands[-1]
    assert commands[-1][-1] == "tests/test_commands.py"

    main = _namespace()["main"]
    with pytest.raises(SystemExit):
        main(
            [
                "parallel-safe",
                "--test-file",
                "tests/test_lease_authority.py",
            ]
        )


def test_parallel_safe_workers_use_invoking_environment_and_exact_selection(monkeypatch) -> None:
    namespace = _namespace()
    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_xdist_preflight", lambda: 0)
    monkeypatch.setitem(main.__globals__, "_run_phase", fake_run)

    assert main(
        [
            "parallel-safe",
            "--workers",
            "4",
            "--test-file",
            "tests/test_commands.py",
        ]
    ) == 0
    command = commands[-1]
    assert command[:10] == (
        namespace["PYTHON"],
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


def test_parallel_safe_reports_xdist_missing_from_primary_environment(monkeypatch, capsys) -> None:
    namespace = _namespace()
    class _Completed:
        returncode = 1

    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: _Completed())
    assert namespace["_xdist_preflight"]() == 3
    assert "install requirements-test.txt" in capsys.readouterr().err


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


def test_parallel_safe_drift_blocks_workers_but_keeps_serial_diagnosis_usable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import test_selection.parallel as parallel

    changed = tmp_path / "tests" / "test_commands.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("# changed after parallel review\n", encoding="utf-8")
    monkeypatch.setattr(parallel, "ROOT", tmp_path)

    namespace = _namespace()
    monkeypatch.setitem(
        namespace["main"].__globals__,
        "_xdist_preflight",
        lambda: pytest.fail("xdist preflight must not run after qualification drift"),
    )
    with pytest.raises(SystemExit) as excinfo:
        namespace["main"](
            [
                "parallel-safe",
                "--workers",
                "4",
                "--test-file",
                "tests/test_commands.py",
            ]
        )
    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    assert "parallel-safe qualification drift" in error
    assert "changed since parallel review" in error
    assert "explicitly update PARALLEL_SAFE_FILE_SHA256" in error

    commands: list[tuple[str, ...]] = []

    def fake_run(phase, *, env):
        commands.append(phase.command)
        return 0

    namespace = _namespace()
    monkeypatch.setitem(namespace["main"].__globals__, "_run_phase", fake_run)
    assert namespace["main"](
        ["parallel-safe", "--test-file", "tests/test_commands.py"]
    ) == 0
    assert "-n" not in commands[-1]
    assert commands[-1][-1] == "tests/test_commands.py"
