from __future__ import annotations

import json
from pathlib import Path

from tests import flake_runner


def test_explicit_random_seeds_must_match_requested_runs():
    assert flake_runner._seeds(2, [11, 22]) == [11, 22]
    try:
        flake_runner._seeds(2, [11])
    except ValueError as exc:
        assert str(exc) == "the number of --seed values must equal --runs"
    else:
        raise AssertionError("mismatched seed count was accepted")


def test_random_order_arguments_record_order_without_per_test_reseeding():
    assert flake_runner._random_order_arguments(101) == [
        "--randomly-seed=101",
        "--randomly-dont-reset-seed",
    ]


def test_static_risk_scan_reports_candidate_signals_without_classifying_flakes(tmp_path):
    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "test_example.py").write_text(
        "import time\nimport threading\n"
        "def test_example(monkeypatch):\n"
        "    monkeypatch.setenv('X', '1')\n"
        "    thread = threading.Thread(target=lambda: None)\n"
        "    thread.start()\n"
        "    thread.join()\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )

    findings = flake_runner.scan_flake_risks(suite)
    kinds = {finding["kind"] for finding in findings}
    assert {"thread", "thread_or_process_join", "real_sleep", "environment"} <= kinds


def test_scan_command_writes_machine_and_human_readable_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(flake_runner, "DEFAULT_ARTIFACT_ROOT", tmp_path)
    artifact = tmp_path / "scan-output"
    args = type("Args", (), {"artifacts": str(artifact)})()

    assert flake_runner.run_scan(args) == 0
    findings = json.loads((artifact / "risks.json").read_text(encoding="utf-8"))
    summary = json.loads((artifact / "summary.json").read_text(encoding="utf-8"))
    assert isinstance(findings, list)
    assert summary["mode"] == "scan"
    assert summary["passed"] is True
    assert (artifact / "risks.md").read_text(encoding="utf-8").startswith(
        "# Flake risk scan"
    )


def test_optional_requirement_layers_keep_xdist_out_of_serial_bootstrap():
    flake = (flake_runner.ROOT / "requirements-flake.txt").read_text(encoding="utf-8")
    parallel = (flake_runner.ROOT / "requirements-parallel.txt").read_text(encoding="utf-8")
    deterministic = (flake_runner.ROOT / "requirements-test.txt").read_text(encoding="utf-8")

    assert "-r requirements-parallel.txt" in flake
    assert "pytest-rerunfailures==16.4" in flake
    assert "pytest-randomly==4.1.0" in flake
    assert "pytest-repeat==0.9.4" in flake
    assert "pytest-xdist==3.8.0" in parallel
    assert "-r requirements-test.txt" in parallel
    assert "pytest-xdist" not in deterministic

    metadata = flake_runner._environment_metadata("unit-test")
    assert metadata["requirements_parallel_sha256"] == flake_runner._hash_file(
        flake_runner.ROOT / "requirements-parallel.txt"
    )


def test_rerun_detector_disables_random_order_and_fails_on_pass_after_rerun(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run_command(**kwargs):
        captured.update(kwargs)
        return flake_runner.CommandResult(
            label=kwargs["label"], command=["pytest"], returncode=0
        )

    monkeypatch.setattr(flake_runner, "_run_command", fake_run_command)
    monkeypatch.setattr(flake_runner, "_write_summary", lambda **_kwargs: 0)
    args = type(
        "Args",
        (),
        {"artifacts": str(tmp_path / "rerun"), "reruns": 2, "pytest_args": []},
    )()

    assert flake_runner.run_rerun_detect(args) == 0
    assert captured["arguments"] == [
        "-p",
        "no:randomly",
        "--reruns",
        "2",
        "--fail-on-flaky",
        "--rerun-show-tracebacks",
    ]


def test_stress_lane_uses_fresh_seeded_process_commands(tmp_path, monkeypatch):
    calls = []

    def fake_run_command(**kwargs):
        calls.append(kwargs)
        return flake_runner.CommandResult(
            label=kwargs["label"],
            command=["pytest"],
            returncode=0,
            seed=kwargs["seed"],
        )

    monkeypatch.setattr(flake_runner, "_run_command", fake_run_command)
    monkeypatch.setattr(flake_runner, "_write_summary", lambda **_kwargs: 0)
    args = type(
        "Args",
        (),
        {
            "artifacts": str(tmp_path / "stress"),
            "runs": 2,
            "seed": [101, 202],
            "marker": "flake_stress",
            "pytest_args": [],
        },
    )()

    assert flake_runner.run_stress(args) == 0
    assert [call["seed"] for call in calls] == [101, 202]
    assert calls[0]["arguments"] == [
        "-m",
        "flake_stress",
        "--randomly-seed=101",
        "--randomly-dont-reset-seed",
    ]
