from __future__ import annotations

import runpy
from pathlib import Path
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]
TEST_UNIT = ROOT / "deploy/systemd/dish-shadow-worker-test.service"
TEST_POSTGRES_UNIT = ROOT / "deploy/systemd/dish-postgres-test.service"
TEST_WORKER_ENV = ROOT / "deploy/systemd/dark-launch-test.env.example"
TEST_SERVICE_ENV = ROOT / "deploy/systemd/service-test.env.example"
TEST_SERVICE_UNIT = ROOT / "deploy/systemd/dish-service-test.service"
TEST_FRONTEND_CADDY_UNIT = ROOT / "deploy/systemd/dish-frontend-test-caddy.service"
TEST_FRONTEND_CADDY_ENV = ROOT / "deploy/systemd/frontend-test-caddy.env.example"
TEST_FRONTEND_CADDY = ROOT / "deploy/caddy/dish-frontend-test.Caddyfile"
LEGACY_SERVICE_ENV = ROOT / "deploy/systemd/service-test-legacy.env.example"
LEGACY_SERVICE_UNIT = ROOT / "deploy/systemd/dish-service-test-legacy.service"
COMPARATOR_RUNBOOK = ROOT / "docs/test-dual-stack-comparator.md"
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


def test_test_postgres_unit_preserves_existing_compose_volume_identity() -> None:
    unit = TEST_POSTGRES_UNIT.read_text(encoding="utf-8")

    assert "docker compose -p postgresql " in unit
    assert "docker compose -p dish-postgres-test " not in unit


def test_test_service_is_pg_authority_and_legacy_oracle_is_isolated() -> None:
    authority = _assignments(TEST_SERVICE_ENV)
    oracle = _assignments(LEGACY_SERVICE_ENV)
    unit = TEST_SERVICE_UNIT.read_text(encoding="utf-8")
    oracle_unit = LEGACY_SERVICE_UNIT.read_text(encoding="utf-8")

    assert authority["DISH_AUTHORITY_BACKEND"] == "postgresql"
    assert authority["DISH_PROFILE"] == "test"
    assert authority["DISH_SERVICE_PORT"] == "8765"
    assert authority["DISH_ACTION_PORT"] == "8766"
    assert authority["DISH_DARK_LAUNCH_MODE"] == "off"
    assert authority["DISH_PG_EXPECTED_SCHEMA_HEAD"] == "0042_scalar_dish_state"
    assert authority["DISH_PG_EXPECTED_DATABASE_NAME"].endswith("_test")
    assert authority["DISH_PG_AUTHORITY_STATE_DIR"].startswith("/home/marco/.local/state/dish/test/")
    assert authority["DISH_FRONTEND_ENABLED"] == "1"
    assert authority["DISH_FRONTEND_POSTGRESQL_READS_ENABLED"] == "1"
    assert authority["DISH_FRONTEND_ORIGIN"].startswith("https://")
    assert authority["DISH_ACTION_PUBLIC_ORIGIN"].startswith("https://")
    assert (
        urlsplit(authority["DISH_FRONTEND_ORIGIN"]).hostname
        != urlsplit(authority["DISH_ACTION_PUBLIC_ORIGIN"]).hostname
    )
    assert (
        urlsplit(authority["DISH_FRONTEND_DATABASE_URL"]).path
        != urlsplit(authority["DISH_FRONTEND_OBSERVATION_DATABASE_URL"]).path
    )
    assert urlsplit(authority["DISH_FRONTEND_OBSERVATION_DATABASE_URL"]).path == (
        "/" + authority["DISH_PG_EXPECTED_DATABASE_NAME"]
    )
    assert not any("ASANA" in name.upper() for name in authority)
    assert "DISH_COOKING_PROJECT_GID" not in authority
    assert "dish-postgres-test.service" in unit
    assert "Conflicts=dish-service.service dish-shadow-worker-test.service" in unit

    assert oracle["DISH_AUTHORITY_BACKEND"] == "legacy"
    assert oracle["DISH_TEST_COMPARATOR_DISPOSABLE"] == "1"
    assert oracle["DISH_COOKING_PROJECT_GID"] == "1216693403164366"
    assert oracle["DISH_SERVICE_PORT"] == "8795"
    assert oracle["DISH_ACTION_PORT"] == "8796"
    assert oracle["DISH_DARK_LAUNCH_MODE"] == "off"
    assert oracle["DISH_DB_PATH"].startswith("/home/marco/.local/state/dish/test-legacy/")
    assert oracle["DISH_SERVICE_BACKUP_DIR"].startswith("/home/marco/.local/state/dish/test-legacy/")
    assert oracle["ASANA_ENV"]
    assert oracle["DISH_SERVICE_ACTION_TOKEN"] != authority["DISH_SERVICE_ACTION_TOKEN"]
    assert "EnvironmentFile=/home/marco/.config/dish-service/test-legacy.env" in oracle_unit
    assert "ReadWritePaths=/home/marco/.local/state/dish/test-legacy" in oracle_unit
    assert "dish-service-test.service" not in oracle_unit.split("Conflicts=", 1)[-1].splitlines()[0]


def test_test_frontend_caddy_is_dedicated_to_existing_private_listener() -> None:
    caddy = TEST_FRONTEND_CADDY.read_text(encoding="utf-8")
    unit = TEST_FRONTEND_CADDY_UNIT.read_text(encoding="utf-8")
    edge = _assignments(TEST_FRONTEND_CADDY_ENV)

    assert "https://{$DISH_FRONTEND_TEST_HOST}" in caddy
    assert "bind {$DISH_FRONTEND_TEST_BIND_IP}" in caddy
    assert "tls {$DISH_FRONTEND_TEST_CERT_FILE} {$DISH_FRONTEND_TEST_KEY_FILE}" in caddy
    assert 'Strict-Transport-Security "max-age=63072000"' in caddy
    assert "reverse_proxy 127.0.0.1:8765" in caddy
    assert "8766" not in caddy
    assert "4183" not in caddy

    assert "Requires=dish-service-test.service" in unit
    assert "BindsTo=dish-service-test.service" in unit
    assert "dish-frontend-private.service" not in unit
    assert "frontend-test-caddy.env" in unit
    assert "dish-frontend-test.Caddyfile" in unit
    assert "CAP_NET_BIND_SERVICE" in unit
    assert set(edge) == {
        "DISH_FRONTEND_TEST_HOST",
        "DISH_FRONTEND_TEST_BIND_IP",
        "DISH_FRONTEND_TEST_CERT_FILE",
        "DISH_FRONTEND_TEST_KEY_FILE",
    }


def test_comparator_qualification_stops_legacy_to_pg_shadow_synchronization() -> None:
    runbook = COMPARATOR_RUNBOOK.read_text(encoding="utf-8")
    assert "systemctl disable --now dish-shadow-worker-test.service" in runbook
    assert "no alternate upstream, load balancing, or automatic fallback" in runbook.lower()
    assert "Do not copy PostgreSQL state into legacy" in runbook

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


def test_prepare_requires_and_canonicalizes_explicit_workflow_section_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(PREPARE))
    for name in namespace["REQUIRED_ENV"]:
        monkeypatch.setenv(name, "fixture")
    monkeypatch.delenv("DISH_PG_RESEARCH_QUEUE_SECTION_GID")

    with pytest.raises(namespace["PrepareError"], match="DISH_PG_RESEARCH_QUEUE_SECTION_GID"):
        namespace["require_env"]("test")

    monkeypatch.setenv("DISH_PG_RESEARCH_QUEUE_SECTION_GID", "fixture")
    monkeypatch.delenv("DISH_PG_VERIFICATION_QUEUE_SECTION_GID")
    with pytest.raises(
        namespace["PrepareError"], match="DISH_PG_VERIFICATION_QUEUE_SECTION_GID"
    ):
        namespace["require_env"]("test")

    monkeypatch.setenv("DISH_PG_VERIFICATION_QUEUE_SECTION_GID", "fixture")
    monkeypatch.delenv("DISH_PG_SOURCING_SECTION_GID")
    with pytest.raises(
        namespace["PrepareError"], match="DISH_PG_SOURCING_SECTION_GID"
    ):
        namespace["require_env"]("test")

    monkeypatch.setenv("DISH_PG_SOURCING_SECTION_GID", "fixture")
    monkeypatch.delenv("DISH_PG_REFERENCE_SECTION_GID")
    with pytest.raises(
        namespace["PrepareError"], match="DISH_PG_REFERENCE_SECTION_GID"
    ):
        namespace["require_env"]("test")

    section_id = namespace["resolve_section_id"]("1216891250619908")
    from dish_pg.location_manifest import target_uuid

    assert section_id == str(target_uuid("section", "1216891250619908"))
    source = PREPARE.read_text(encoding="utf-8")
    assert '"--research-queue-section-id", research_queue_section_id' in source
    assert '"--research-queue-section-gid", research_queue_section_gid' in source
    assert '"--verification-queue-section-id", verification_queue_section_id' in source
    assert '"--verification-queue-section-gid", verification_queue_section_gid' in source
    assert '"--sourcing-section-id", sourcing_section_id' in source
    assert '"--sourcing-section-gid", sourcing_section_gid' in source
    assert '"--reference-section-id", reference_section_id' in source
    assert '"--reference-section-gid", reference_section_gid' in source
    with pytest.raises(namespace["PrepareError"], match="canonical positive decimal Asana GID"):
        namespace["resolve_section_id"]("Research Queue")


def test_postgresql_service_units_stop_restarting_on_deterministic_exit() -> None:
    root = ROOT / "deploy/systemd"
    for name in (
        "dish-service.service",
        "dish-service-test.service",
        "dish-service-prod.service",
        "dish-shadow-worker.service",
        "dish-shadow-worker-test.service",
        "dish-service-test-legacy.service",
    ):
        unit = (root / name).read_text(encoding="utf-8")
        assert "Restart=on-failure" in unit
        assert "RestartPreventExitStatus=78" in unit

    for name in ("dish-shadow-worker.service", "dish-shadow-worker-test.service"):
        unit = (root / name).read_text(encoding="utf-8")
        assert "ExecStartPre=" not in unit
        assert "-m dish_pg.shadow_worker_entrypoint" in unit
