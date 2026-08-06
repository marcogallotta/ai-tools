from __future__ import annotations

import json
import runpy
import subprocess
import uuid
from pathlib import Path

import pytest

from dish_pg.dark_launch_readiness import (
    DarkLaunchReadinessError,
    inspect_worker_unit,
    parse_systemctl_show,
)
from dish_service.shadow_spool import ShadowSpool, ShadowSpoolError
from tests.support.postgresql.dark_launch_readiness import ROOT, UNIT


def _systemd_fixture(tmp_path: Path):
    installed = tmp_path / "dish-shadow-worker.service"
    installed.write_bytes(UNIT.read_bytes())
    installed.chmod(0o644)
    worker_environment = tmp_path / "dark-launch.env"
    worker_environment.write_text("fixture=only\n", encoding="utf-8")
    worker_environment.chmod(0o600)
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(
                (
                    "LoadState=loaded",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "UnitFileState=disabled",
                    f"FragmentPath={installed}",
                    "Result=success",
                    f"EnvironmentFiles={worker_environment} (ignore_errors=no)",
                    "Environment=",
                    "PassEnvironment=",
                    "DropInPaths=",
                )
            ),
            stderr="",
        )

    return installed, worker_environment, calls, runner


def _inspect(worker_environment: Path, runner):
    return inspect_worker_unit(
        unit_name="dish-shadow-worker.service",
        repository_unit=UNIT,
        expected_environment_file=worker_environment,
        runner=runner,
    )


def test_systemd_inspection_uses_one_bounded_read_only_show_command(tmp_path: Path) -> None:
    _installed, worker_environment, calls, runner = _systemd_fixture(tmp_path)
    report = _inspect(worker_environment, runner)
    assert report["passed"] is True
    assert calls == [
        [
            "systemctl",
            "show",
            "dish-shadow-worker.service",
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState,FragmentPath,Result,"
            "EnvironmentFiles,Environment,PassEnvironment,DropInPaths",
        ]
    ]
    assert parse_systemctl_show("ActiveState=active\nSubState=running\n") == {
        "ActiveState": "active",
        "SubState": "running",
    }


@pytest.mark.parametrize(
    "replacements",
    [
        (("ActiveState=inactive", "ActiveState=active"), ("SubState=dead", "SubState=running")),
        (("UnitFileState=disabled", "UnitFileState=enabled"),),
        (
            ("ActiveState=inactive", "ActiveState=failed"),
            ("SubState=dead", "SubState=failed"),
            ("Result=success", "Result=exit-code"),
        ),
    ],
)
def test_systemd_inspection_rejects_active_enabled_or_failed_state(
    tmp_path: Path, replacements
) -> None:
    _installed, worker_environment, _calls, runner = _systemd_fixture(tmp_path)

    def changed_runner(command, **kwargs):
        result = runner(command, **kwargs)
        for old, new in replacements:
            result.stdout = result.stdout.replace(old, new)
        return result

    assert _inspect(worker_environment, changed_runner)["passed"] is False


def test_systemd_inspection_rejects_divergent_unit_and_drop_ins(tmp_path: Path) -> None:
    installed, worker_environment, _calls, runner = _systemd_fixture(tmp_path)
    installed.write_text("[Unit]\nDescription=divergent\n", encoding="utf-8")
    divergent = _inspect(worker_environment, runner)
    assert divergent["passed"] is False
    assert divergent["details"]["digest_matches"] is False

    installed.write_bytes(UNIT.read_bytes())

    def drop_in_runner(command, **kwargs):
        result = runner(command, **kwargs)
        result.stdout = result.stdout.replace(
            "DropInPaths=",
            "DropInPaths=/etc/systemd/system/dish-shadow-worker.service.d/override.conf",
        )
        return result

    drop_in = _inspect(worker_environment, drop_in_runner)
    assert drop_in["passed"] is False
    assert drop_in["details"]["drop_in_paths"]


def test_systemd_inspection_rejects_environment_injection_and_unbounded_output(
    tmp_path: Path,
) -> None:
    _installed, worker_environment, _calls, runner = _systemd_fixture(tmp_path)

    def injected_environment_runner(command, **kwargs):
        result = runner(command, **kwargs)
        result.stdout = result.stdout.replace(
            "Environment=", "Environment=ASANA_PAT=must-not-be-reported"
        )
        return result

    injected = _inspect(worker_environment, injected_environment_runner)
    assert injected["passed"] is False
    assert injected["details"]["inline_environment_present"] is True
    assert "must-not-be-reported" not in json.dumps(injected, sort_keys=True)

    other_environment = tmp_path / "other.env"
    other_environment.write_text("fixture=other\n", encoding="utf-8")
    other_environment.chmod(0o600)

    def additional_environment_runner(command, **kwargs):
        result = runner(command, **kwargs)
        result.stdout = result.stdout.replace(
            f"EnvironmentFiles={worker_environment} (ignore_errors=no)",
            (
                f"EnvironmentFiles={worker_environment} (ignore_errors=no) "
                f"{other_environment} (ignore_errors=no)"
            ),
        )
        return result

    additional = _inspect(worker_environment, additional_environment_runner)
    assert additional["passed"] is False
    assert additional["details"]["environment_file_matches"] is False

    def oversized_runner(command, **kwargs):
        result = runner(command, **kwargs)
        result.stdout += "\nEnvironment=" + ("x" * (128 * 1024))
        return result

    with pytest.raises(DarkLaunchReadinessError, match="bounded limit"):
        _inspect(worker_environment, oversized_runner)


def test_shadow_worker_restart_harness_supplies_expected_database_identity(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(ROOT / "scripts/dish-pg-certify-shadow-worker-restart"),
        run_name="dish_pg_restart_contract",
    )
    command = namespace["worker_command"](
        database_url="postgresql+psycopg://dish:secret@localhost/dish_stage_a_dark_test",
        spool_path=tmp_path / "spool.sqlite3",
        baseline_id=uuid.uuid4(),
        worker_id="restart-cert-worker",
        secret_path=tmp_path / "cursor-secret",
        kill_switch_path=tmp_path / "dark-launch.disabled",
        reservation_ttl_seconds=90,
    )
    expected_index = command.index("--expected-database-name")
    assert command[expected_index + 1] == "dish_stage_a_dark_test"


def test_read_only_spool_inspection_does_not_change_database_bytes(tmp_path: Path) -> None:
    path = tmp_path / "spool.sqlite3"
    spool = ShadowSpool(path)
    spool.status()
    before = path.read_bytes()
    before_names = sorted(item.name for item in tmp_path.iterdir())
    report = ShadowSpool.open_existing_read_only(path).status()
    assert report["counts"]["reserved"] == 0
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == before_names

    missing = tmp_path / "missing-spool.sqlite3"
    with pytest.raises(ShadowSpoolError, match="does not exist"):
        ShadowSpool.open_existing_read_only(missing).status()
    assert not missing.exists()


def test_strict_read_only_spool_rejects_uncheckpointed_wal(tmp_path: Path) -> None:
    path = tmp_path / "spool.sqlite3"
    ShadowSpool(path).status()
    Path(f"{path}-wal").write_bytes(b"pending-wal")
    with pytest.raises(ShadowSpoolError, match="quiescent checkpointed"):
        ShadowSpool.open_existing_read_only(path)
