from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dish_pg.dark_launch_readiness import (
    READINESS_CHECK_NAMES,
    inspect_worker_unit,
    parse_environment_file,
    run_preflight,
)
from tests.support.postgresql.core import NOW
from tests.support.postgresql.dark_launch_readiness import (
    DisposableEngine,
    ROOT,
    UNIT,
    passing_database_checks,
    preflight_fixture,
    systemctl_runner,
    write_environment,
)


def test_preflight_fixture_report_is_complete_bounded_and_redacted(
    tmp_path: Path, monkeypatch
) -> None:
    inputs, installed, database_password, service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    engine = DisposableEngine()
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: engine,
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["read_only"] is True
    assert report["production_mutated"] is False
    assert set(report["checks"]) == set(READINESS_CHECK_NAMES)
    assert all(
        set(("passed", "status", "reason")).issubset(check)
        for check in report["checks"].values()
    )
    capacity = report["checks"]["spool"]["details"]["capacity"]
    assert capacity["max_bytes"] == 536_870_912
    assert capacity["max_records"] == 100_000
    assert capacity["min_free_bytes"] == 1_073_741_824
    service_contract = report["checks"]["service_environment"]["details"]
    assert service_contract["spool_path"] == str(inputs.spool_path.resolve())
    assert service_contract["kill_switch_path"] == str(inputs.kill_switch.resolve())
    assert service_contract["numeric_limits"][
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS"
    ] == 100_000
    serialized = json.dumps(report, sort_keys=True)
    assert database_password not in serialized
    assert service_token not in serialized
    assert len(serialized) < 40_000
    assert inputs.report_path is not None
    assert json.loads(inputs.report_path.read_text(encoding="utf-8"))["ready"] is True
    assert inputs.report_path.stat().st_mode & 0o777 == 0o600
    assert engine.disposed is True


def test_preflight_rejects_alternate_service_environment(
    tmp_path: Path, monkeypatch
) -> None:
    inputs, installed, _database_password, _service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    required_environment = inputs.service_environment
    alternate_environment = inputs.config_root / "alternate-prod.env"
    alternate_environment.write_bytes(required_environment.read_bytes())
    alternate_environment.chmod(0o600)
    inputs = inputs.__class__(
        **{**inputs.__dict__, "service_environment": alternate_environment}
    )
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is False
    assert report["checks"]["service_environment"]["passed"] is False
    assert report["checks"]["filesystem_isolation"]["passed"] is False
    assert str(required_environment) in report["checks"]["service_environment"]["reason"]


@pytest.mark.parametrize(
    "missing_name",
    ["DISH_DARK_LAUNCH_SPOOL_PATH", "DISH_DARK_LAUNCH_KILL_SWITCH"],
)
def test_preflight_rejects_missing_service_dark_launch_paths(
    tmp_path: Path, monkeypatch, missing_name: str
) -> None:
    inputs, installed, _database_password, _service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    service_values = parse_environment_file(
        inputs.service_environment, label="fixture service environment"
    )
    service_values.pop(missing_name)
    write_environment(inputs.service_environment, service_values)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is False
    assert report["checks"]["service_environment"]["passed"] is False
    assert missing_name in report["checks"]["service_environment"]["reason"]


def test_preflight_rejects_service_worker_limit_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    inputs, installed, _database_password, _service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    service_values = parse_environment_file(
        inputs.service_environment, label="fixture service environment"
    )
    service_values["DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS"] = "99999"
    write_environment(inputs.service_environment, service_values)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is False
    assert report["checks"]["service_environment"]["passed"] is False
    assert "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS" in report["checks"][
        "service_environment"
    ]["reason"]


def test_preflight_rejects_spool_capacity_breach(tmp_path: Path, monkeypatch) -> None:
    inputs, installed, _database_password, _service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    worker_text = inputs.worker_environment.read_text(encoding="utf-8")
    inputs.worker_environment.write_text(
        worker_text.replace(
            "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES=536870912",
            "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES=1",
        ),
        encoding="utf-8",
    )
    inputs.worker_environment.chmod(0o600)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    spool_check = report["checks"]["spool"]
    assert report["status"] == "not_ready"
    assert spool_check["passed"] is False
    assert spool_check["details"]["capacity"]["accepting_new_records"] is False
    assert "capacity or free-space limit" in spool_check["reason"]


def test_preflight_never_writes_an_unvalidated_report_path(
    tmp_path: Path, monkeypatch
) -> None:
    inputs, installed, _database_password, _service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )
    unsafe_report = tmp_path / "outside-readiness.json"
    inputs = inputs.__class__(**{**inputs.__dict__, "report_path": unsafe_report})
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only",
        lambda **_kwargs: passing_database_checks(),
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is False
    assert report["checks"]["filesystem_isolation"]["passed"] is False
    assert report["checks"]["report_output"]["passed"] is False
    assert "no report file was written" in report["checks"]["report_output"]["reason"]
    assert not unsafe_report.exists()


def test_preflight_unavailable_database_populates_every_check_and_redacts(
    tmp_path: Path, monkeypatch
) -> None:
    inputs, installed, database_password, service_token = preflight_fixture(tmp_path)
    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.PRODUCTION_SERVICE_ENVIRONMENT",
        inputs.service_environment,
    )

    def unavailable(**_kwargs):
        raise RuntimeError(f"cannot connect {inputs.database_url} {service_token}")

    monkeypatch.setattr(
        "dish_pg.dark_launch_readiness.inspect_postgresql_read_only", unavailable
    )
    report = run_preflight(
        inputs,
        engine_factory=lambda _settings: DisposableEngine(),
        systemctl_runner=systemctl_runner(installed, inputs.worker_environment),
        now=lambda: NOW,
    )
    assert report["ready"] is False
    assert report["status"] == "blocked"
    assert set(report["checks"]) == set(READINESS_CHECK_NAMES)
    assert report["checks"]["postgresql_connectivity"]["status"] == "unavailable"
    assert report["checks"]["database_identity"]["status"] == "unavailable"
    serialized = json.dumps(report, sort_keys=True)
    assert database_password not in serialized
    assert service_token not in serialized
    assert "<redacted>" in serialized


def test_missing_installed_worker_unit_is_a_failed_readiness_check() -> None:
    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(
                (
                    "LoadState=not-found",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "UnitFileState=",
                    "FragmentPath=",
                    "Result=success",
                    "EnvironmentFiles=",
                    "Environment=",
                    "PassEnvironment=",
                    "DropInPaths=",
                )
            ),
            stderr="",
        )

    report = inspect_worker_unit(
        unit_name="dish-shadow-worker.service",
        repository_unit=UNIT,
        expected_environment_file=UNIT,
        runner=runner,
    )
    assert report["passed"] is False
    assert report["status"] == "fail"
    assert report["details"]["load_state"] == "not-found"


def _cli_help() -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    commands = {
        "readiness": [sys.executable, "scripts/dish-pg-dark-launch-readiness", "--help"],
        "status": [sys.executable, "scripts/dish-pg-dark-launch", "status", "--help"],
        "manifest": [sys.executable, "scripts/dish-pg-build-location-manifest", "--help"],
        "export": [sys.executable, "scripts/dish-pg-export-legacy", "--help"],
        "bootstrap": [sys.executable, "scripts/dish-pg-bootstrap-initial", "--help"],
        "import": [sys.executable, "scripts/dish-pg-import-legacy", "--help"],
        "baseline": [
            sys.executable,
            "scripts/dish-pg-dark-launch",
            "baseline-create",
            "--help",
        ],
        "epoch": [
            sys.executable,
            "scripts/dish-pg-dark-launch",
            "activate-epoch",
            "--help",
        ],
        "disable": [
            sys.executable,
            "scripts/dish-pg-dark-launch",
            "disable",
            "--help",
        ],
        "resume": [
            sys.executable,
            "scripts/dish-pg-dark-launch",
            "enable-capture",
            "--help",
        ],
    }
    return {
        name: subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        for name, command in commands.items()
    }


def _production_runbook() -> tuple[str, str]:
    runbook = (ROOT / "docs/database-backend-dark-launch-runbook.md").read_text(
        encoding="utf-8"
    )
    return runbook, runbook.split("## TEST dark-launch acceptance sequence", 1)[0]


def test_runbook_readiness_and_status_commands_match_cli_help() -> None:
    _runbook, production_section = _production_runbook()
    help_text = _cli_help()
    assert "dish_stage_a_dark_test" not in production_section

    readiness_flags = (
        "--service-environment",
        "--worker-environment",
        "--database-url",
        "--expected-database-name",
        "--manifest",
        "--legacy-ndjson",
        "--bootstrap-receipt",
        "--spool-path",
        "--kill-switch",
        "--unit-name",
        "--repository-unit",
        "--report-path",
    )
    status_flags = (
        "--database-url",
        "--spool-path",
        "--baseline-id",
        "--kill-switch",
        "--worker-unit",
        "--warning-backlog",
        "--critical-backlog",
        "--warning-lag-seconds",
        "--critical-lag-seconds",
        "--warning-capacity-percent",
        "--critical-capacity-percent",
        "--warning-mismatches",
        "--critical-mismatches",
        "--warning-gaps",
        "--critical-gaps",
    )
    assert all(
        flag in help_text["readiness"] and flag in production_section
        for flag in readiness_flags
    )
    assert all(
        flag in help_text["status"] and flag in production_section
        for flag in status_flags
    )


def test_runbook_operational_commands_match_cli_help() -> None:
    runbook, production_section = _production_runbook()
    help_text = _cli_help()
    documented_flags = {
        "manifest": ("--env-file", "--output"),
        "export": ("--database", "--location-manifest", "--output"),
        "bootstrap": (
            "--database-url",
            "--expected-database-name",
            "--source",
            "--source-generation",
            "--dish-repo",
            "--dish-commit",
            "--honest-repo",
            "--honest-commit",
            "--receipt",
        ),
        "import": (
            "--database-url",
            "--source",
            "--expected-source-sha256",
            "--expected-record-count",
            "--generation-id",
            "--import-run-id",
            "--contract-binding-id",
        ),
        "baseline": (
            "--database-url",
            "--spool-path",
            "--generation-id",
            "--source-generation",
            "--source-commit",
        ),
        "epoch": ("--database-url", "--generation-id", "--reason"),
        "disable": ("--kill-switch", "--reason"),
        "resume": ("--kill-switch",),
    }
    assert all(
        flag in help_text[command_name] and flag in production_section
        for command_name, flags in documented_flags.items()
        for flag in flags
    )
    assert "current checkout's TEST-only implementation is an integration blocker" not in runbook
    assert "--environment production" in production_section
    assert "fixed production service environment" in runbook
