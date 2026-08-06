from __future__ import annotations

import os
from pathlib import Path

import pytest

from dish_pg.dark_launch_readiness import (
    DarkLaunchReadinessError,
    PRODUCTION_SERVICE_ENVIRONMENT,
    execstart_variables,
    validate_production_paths,
    validate_service_dark_launch_configuration,
    validate_worker_environment,
)
from tests.support.postgresql.dark_launch_readiness import (
    UNIT,
    example_assignments,
    path_fixture,
    valid_worker_values,
    write_owner_file,
)


def _validate_fixture_paths(*, service_values, worker_values, inputs):
    return validate_production_paths(
        service_values=service_values,
        worker_values=worker_values,
        inputs=inputs,
        expected_service_environment=inputs.service_environment,
    )


def test_production_service_environment_identity_is_fixed() -> None:
    assert PRODUCTION_SERVICE_ENVIRONMENT == Path(
        "/home/marco/.config/dish-service/prod.env"
    )


def test_committed_worker_unit_and_environment_example_are_synchronized(tmp_path: Path) -> None:
    unit_variables = set(execstart_variables(UNIT.read_text(encoding="utf-8")))
    example_variables = set(example_assignments())
    assert unit_variables == example_variables

    contract = validate_worker_environment(
        valid_worker_values(tmp_path), unit_text=UNIT.read_text(encoding="utf-8")
    )
    assert set(contract["execstart_variables"]) == unit_variables
    assert contract["database_name"] == "dish_prod"
    assert contract["credential_variables_present"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"DISH_DARK_LAUNCH_KILL_SWITCH": "replace-with-marker"}, "placeholder"),
        ({"DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS": "89"}, "at least 90"),
        ({"DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS": "60"}, "at least the reservation"),
        ({"DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS": "0"}, "positive integer"),
        ({"DISH_PG_EXPECTED_DATABASE_NAME": "other"}, "does not match"),
        ({"ASANA_PAT": "secret"}, "prohibited credential"),
        ({"DISH_SERVICE_ADMIN_TOKEN": "secret"}, "prohibited credential"),
        ({"DISH_SERVICE_TOKEN": "secret"}, "prohibited credential"),
        ({"ASANA_ACCESS_TOKEN": "secret"}, "prohibited credential"),
        ({"PROJECTION_ADAPTER_SECRET": "secret"}, "prohibited credential"),
        ({"PYTHONPATH": "/tmp/injected"}, "unsupported variables"),
        ({"LD_PRELOAD": "/tmp/injected.so"}, "unsupported variables"),
        ({"DISH_DARK_LAUNCH_WORKER_ID": "worker with spaces"}, "whitespace or NUL"),
    ],
)
def test_worker_environment_fails_closed(
    tmp_path: Path, mutation: dict[str, str], message: str
) -> None:
    values = valid_worker_values(tmp_path)
    values.update(mutation)
    with pytest.raises(DarkLaunchReadinessError, match=message):
        validate_worker_environment(values, unit_text=UNIT.read_text(encoding="utf-8"))


def test_worker_environment_rejects_missing_execstart_variable(tmp_path: Path) -> None:
    values = valid_worker_values(tmp_path)
    values.pop("DISH_DARK_LAUNCH_KILL_SWITCH")
    with pytest.raises(DarkLaunchReadinessError, match="missing ExecStart variables"):
        validate_worker_environment(values, unit_text=UNIT.read_text(encoding="utf-8"))


def test_service_effective_dark_launch_configuration_matches_worker_and_inputs(
    tmp_path: Path,
) -> None:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    details = validate_service_dark_launch_configuration(
        service_values=service_values,
        worker_values=worker_values,
        inputs=inputs,
    )
    assert details["spool_path"] == str(inputs.spool_path.resolve())
    assert details["kill_switch_path"] == str(inputs.kill_switch.resolve())
    assert details["numeric_limits"] == {
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS": 50,
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES": 536_870_912,
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS": 100_000,
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES": 1_073_741_824,
    }


def test_service_effective_limits_use_service_config_defaults(tmp_path: Path) -> None:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    for name in (
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS",
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES",
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS",
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES",
    ):
        service_values.pop(name)
    details = validate_service_dark_launch_configuration(
        service_values=service_values,
        worker_values=worker_values,
        inputs=inputs,
    )
    assert details["numeric_limits"]["DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS"] == 50
    assert (
        details["numeric_limits"]["DISH_DARK_LAUNCH_MAX_SPOOL_BYTES"]
        == 536_870_912
    )


@pytest.mark.parametrize(
    "missing_name",
    ["DISH_DARK_LAUNCH_SPOOL_PATH", "DISH_DARK_LAUNCH_KILL_SWITCH"],
)
def test_service_effective_dark_launch_paths_must_be_explicit(
    tmp_path: Path, missing_name: str
) -> None:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    service_values.pop(missing_name)
    with pytest.raises(DarkLaunchReadinessError, match=missing_name):
        validate_service_dark_launch_configuration(
            service_values=service_values,
            worker_values=worker_values,
            inputs=inputs,
        )


def test_service_and_worker_effective_limits_must_match(tmp_path: Path) -> None:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    worker_values["DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS"] = "99999"
    with pytest.raises(
        DarkLaunchReadinessError, match="DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS"
    ):
        validate_service_dark_launch_configuration(
            service_values=service_values,
            worker_values=worker_values,
            inputs=inputs,
        )


def test_production_path_contract_rejects_alias_symlink_permissions_and_test_root(
    tmp_path: Path,
) -> None:
    inputs, service_values, worker_values = path_fixture(tmp_path)
    result = _validate_fixture_paths(
        service_values=service_values, worker_values=worker_values, inputs=inputs
    )
    assert result["spool"] == str(inputs.spool_path.resolve())

    inputs.manifest.chmod(0o644)
    with pytest.raises(DarkLaunchReadinessError, match="owner-only"):
        _validate_fixture_paths(
            service_values=service_values, worker_values=worker_values, inputs=inputs
        )
    inputs.manifest.chmod(0o600)

    alias = inputs.evidence_root / "manifest-hardlink.json"
    os.link(inputs.manifest, alias)
    with pytest.raises(DarkLaunchReadinessError, match="hard links"):
        _validate_fixture_paths(
            service_values=service_values, worker_values=worker_values, inputs=inputs
        )
    alias.unlink()

    original = inputs.manifest
    target = inputs.evidence_root / "manifest-target.json"
    original.rename(target)
    original.symlink_to(target)
    with pytest.raises(DarkLaunchReadinessError, match="non-symlink"):
        _validate_fixture_paths(
            service_values=service_values, worker_values=worker_values, inputs=inputs
        )
    original.unlink()
    target.rename(original)

    test_root = tmp_path / "test"
    test_root.mkdir(mode=0o700)
    test_spool = test_root / "dark-launch-spool.sqlite3"
    write_owner_file(test_spool)
    unsafe = inputs.__class__(**{**inputs.__dict__, "spool_path": test_spool})
    unsafe_service = {**service_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(test_spool)}
    unsafe_worker = {**worker_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(test_spool)}
    with pytest.raises(DarkLaunchReadinessError, match="TEST-root"):
        _validate_fixture_paths(
            service_values=unsafe_service, worker_values=unsafe_worker, inputs=unsafe
        )

    aliased_service = {**service_values, "DISH_DB_PATH": str(inputs.spool_path)}
    with pytest.raises(DarkLaunchReadinessError, match="distinct filesystem objects"):
        _validate_fixture_paths(
            service_values=aliased_service, worker_values=worker_values, inputs=inputs
        )

    outside = tmp_path / "outside-spool.sqlite3"
    write_owner_file(outside)
    outside_inputs = inputs.__class__(**{**inputs.__dict__, "spool_path": outside})
    outside_service = {**service_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(outside)}
    outside_worker = {**worker_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(outside)}
    with pytest.raises(DarkLaunchReadinessError, match="approved root"):
        _validate_fixture_paths(
            service_values=outside_service,
            worker_values=outside_worker,
            inputs=outside_inputs,
        )

    real_directory = inputs.state_root / "real"
    real_directory.mkdir(mode=0o700)
    linked_directory = inputs.state_root / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    linked_spool = linked_directory / "spool.sqlite3"
    write_owner_file(real_directory / "spool.sqlite3")
    linked_inputs = inputs.__class__(**{**inputs.__dict__, "spool_path": linked_spool})
    linked_service = {**service_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(linked_spool)}
    linked_worker = {**worker_values, "DISH_DARK_LAUNCH_SPOOL_PATH": str(linked_spool)}
    with pytest.raises(DarkLaunchReadinessError, match="traverse a symlink"):
        _validate_fixture_paths(
            service_values=linked_service,
            worker_values=linked_worker,
            inputs=linked_inputs,
        )
