from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _namespace():
    return runpy.run_path(str(ROOT / "scripts" / "dish-test-lane"))


def test_named_lanes_are_complete_and_obvious() -> None:
    lanes = _namespace()["LANES"]
    assert tuple(sorted(lanes)) == (
        "command-api-contracts",
        "native-concurrency",
        "operational-certification",
        "pglite",
        "release-cutover",
        "schema-migrations",
    )
    assert all(phases for phases in lanes.values())
    assert all(phase.name.strip() and phase.command for phases in lanes.values() for phase in phases)


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
