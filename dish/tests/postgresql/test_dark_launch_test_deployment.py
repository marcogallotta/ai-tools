from __future__ import annotations

import runpy
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TEST_UNIT = ROOT / "deploy/systemd/dish-shadow-worker-test.service"
TEST_WORKER_ENV = ROOT / "deploy/systemd/dark-launch-test.env.example"
TEST_SERVICE_ENV = ROOT / "deploy/systemd/service-test.env.example"
PREPARE = ROOT / "scripts/dish-pg-production-prepare"
TEST_PREPARE = ROOT / "scripts/dish-pg-test-prepare"


def _assignments(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        assert separator, f"invalid environment example line: {raw!r}"
        values[name] = value
    return values


def test_test_worker_unit_is_test_isolated_and_credential_free() -> None:
    unit = TEST_UNIT.read_text(encoding="utf-8")
    worker = _assignments(TEST_WORKER_ENV)

    assert "dish-service-test.service" in unit
    assert "dish-postgres-test.service" in unit
    assert "EnvironmentFile=/home/marco/.config/dish-service/dark-launch-test.env" in unit
    assert "ReadWritePaths=/home/marco/.local/state/dish/test" in unit
    assert "/dish/prod" not in unit
    assert "prod.env" not in unit
    assert set(worker) >= {
        "DISH_PG_DATABASE_URL",
        "DISH_PG_EXPECTED_DATABASE_NAME",
        "DISH_DARK_LAUNCH_SPOOL_PATH",
        "DISH_DARK_LAUNCH_BASELINE_ID",
        "DISH_DARK_LAUNCH_WORKER_ID",
        "DISH_PG_CURSOR_SECRET_FILE",
        "DISH_DARK_LAUNCH_COMPARATOR_RELEASE",
        "DISH_DARK_LAUNCH_KILL_SWITCH",
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS",
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES",
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS",
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES",
        "DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS",
        "DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS",
    }
    assert not any(
        name == "ASANA_ENV"
        or name.startswith("ASANA_")
        or name.startswith("DISH_SERVICE_")
        or "PROJECTION_ADAPTER" in name
        for name in worker
    )
    assert all("/dish/prod" not in value for value in worker.values())


def test_test_service_has_explicit_matching_capture_configuration() -> None:
    service = _assignments(TEST_SERVICE_ENV)
    worker = _assignments(TEST_WORKER_ENV)

    assert service["DISH_DARK_LAUNCH_MODE"] == "off"
    assert service["DISH_DARK_LAUNCH_SOURCE_GENERATION"] != "legacy-sqlite"
    for name in (
        "DISH_DARK_LAUNCH_SPOOL_PATH",
        "DISH_DARK_LAUNCH_KILL_SWITCH",
        "DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS",
        "DISH_DARK_LAUNCH_MAX_SPOOL_BYTES",
        "DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS",
        "DISH_DARK_LAUNCH_MIN_FREE_BYTES",
    ):
        assert service[name] == worker[name]


def test_test_prepare_entrypoint_forces_test_and_target_gate_precedes_mutation() -> None:
    wrapper = TEST_PREPARE.read_text(encoding="utf-8")
    assert 'os.environ["DISH_PG_CAPTURE_ENVIRONMENT"] = "test"' in wrapper

    namespace = runpy.run_path(str(PREPARE))
    validate = namespace["validate_target_identity"]
    values = {
        "DISH_PG_DATABASE_URL": "postgresql+psycopg://dish:secret@localhost/dish_stage_a_prod",
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_stage_a_prod",
        "DISH_DB_PATH": "/home/marco/.local/state/dish/test/shared.sqlite3",
        "DISH_PG_LOCATION_MANIFEST": "/home/marco/.local/state/dish/test/evidence/manifest.json",
        "DISH_PG_LEGACY_NDJSON": "/home/marco/.local/state/dish/test/evidence/legacy.ndjson",
        "DISH_PG_BOOTSTRAP_RECEIPT": "/home/marco/.local/state/dish/test/evidence/receipt.json",
        "DISH_DARK_LAUNCH_SPOOL_PATH": "/home/marco/.local/state/dish/test/dark-launch-spool.sqlite3",
    }
    with pytest.raises(namespace["PrepareError"], match="ending in '_test'"):
        validate("test", values)

    source = PREPARE.read_text(encoding="utf-8")
    assert source.index("validate_target_identity(environment, env)") < source.index(
        'migrate_schema(env["DISH_PG_DATABASE_URL"])'
    )


def test_test_prepare_requires_the_test_service_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(PREPARE))
    for name in namespace["REQUIRED_ENV"]:
        monkeypatch.setenv(name, "fixture")
    monkeypatch.delenv("DISH_TEST_SERVICE_ENV", raising=False)
    monkeypatch.setenv("DISH_PRODUCTION_SERVICE_ENV", "/tmp/prod.env")

    with pytest.raises(namespace["PrepareError"], match="DISH_TEST_SERVICE_ENV"):
        namespace["require_env"]("test")
