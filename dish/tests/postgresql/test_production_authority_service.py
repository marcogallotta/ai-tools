from __future__ import annotations

import os
from pathlib import Path

import pytest

from dish_pg.release import ALEMBIC_HEAD
from dish_service import __main__ as service_main
from dish_tool.errors import DishRuleError


ROOT = Path(__file__).resolve().parents[2]
SYSTEMD = ROOT / "deploy" / "systemd"
PRODUCTION_UNIT = SYSTEMD / "dish-service-postgresql-prod.service"
PRODUCTION_ENV = SYSTEMD / "service-postgresql-prod.env.example"


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


def test_production_postgresql_runtime_profile_is_explicit_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = service_main.build_parser().parse_args(["--postgresql-production-runtime"])

    monkeypatch.setenv("DISH_PROFILE", "prod")
    assert service_main._postgresql_runtime_profile(args) == "prod"

    monkeypatch.setenv("DISH_PROFILE", "test")
    with pytest.raises(DishRuleError) as caught:
        service_main._postgresql_runtime_profile(args)
    assert caught.value.rule == "postgresql_runtime_profile_mismatch"


def test_postgresql_runtime_database_identity_is_profile_scoped() -> None:
    service_main._validate_postgresql_runtime_database(
        profile="prod", expected_database="dish_stage_a_prod"
    )
    service_main._validate_postgresql_runtime_database(
        profile="test", expected_database="dish_stage_a_test"
    )

    with pytest.raises(DishRuleError) as prod_caught:
        service_main._validate_postgresql_runtime_database(
            profile="prod", expected_database="dish_stage_a_test"
        )
    assert prod_caught.value.rule == "postgresql_runtime_database_not_production"

    with pytest.raises(DishRuleError) as test_caught:
        service_main._validate_postgresql_runtime_database(
            profile="test", expected_database="dish_stage_a_prod"
        )
    assert test_caught.value.rule == "postgresql_runtime_database_not_disposable"


def test_production_postgresql_runtime_rejects_reachable_asana_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = service_main.build_parser().parse_args(["--postgresql-production-runtime"])
    monkeypatch.setenv("DISH_PROFILE", "prod")
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "must-not-be-reachable")

    with pytest.raises(DishRuleError) as caught:
        service_main._postgresql_runtime_config(args, profile="prod")

    assert caught.value.rule == "postgresql_runtime_asana_environment_reachable"
    assert caught.value.details == {"environment_keys": ["ASANA_ACCESS_TOKEN"]}


def test_production_postgresql_service_composition_is_no_asana_and_non_cutover() -> None:
    unit = PRODUCTION_UNIT.read_text(encoding="utf-8")
    env = _assignments(PRODUCTION_ENV)

    assert "ExecStart=/home/marco/ai-tools/dish/dish-service --postgresql-production-runtime" in unit
    assert "EnvironmentFile=/home/marco/.config/dish-service/postgresql-prod.env" in unit
    assert "Requires=dish-postgres-prod.service" in unit
    assert "InaccessiblePaths=/home/marco/.config/asana-cli" in unit
    assert "ReadWritePaths=/home/marco/.local/state/dish/prod/pg-authority" in unit
    assert "RestartPreventExitStatus=78" in unit

    # Starting the PostgreSQL authority runtime is not itself cutover authorization.
    assert "Conflicts=dish-service-prod.service" not in unit
    assert "ExecStop" not in unit
    assert "prod.env" not in unit.replace("postgresql-prod.env", "")

    assert env["DISH_AUTHORITY_BACKEND"] == "postgresql"
    assert env["DISH_PROFILE"] == "prod"
    assert env["DISH_PG_EXPECTED_DATABASE_NAME"] == "dish_stage_a_prod"
    assert env["DISH_PG_EXPECTED_SCHEMA_HEAD"] == ALEMBIC_HEAD
    assert env["DISH_PG_AUTHORITY_STATE_DIR"] == "/home/marco/.local/state/dish/prod/pg-authority"

    forbidden_exact = {
        "ASANA_ENV",
        "DISH_DB_PATH",
        "DISH_HONEST_PATH",
        "DISH_COOKING_PROJECT_GID",
        "DISH_DARK_LAUNCH_MODE",
        "DISH_DARK_LAUNCH_SPOOL_PATH",
    }
    assert forbidden_exact.isdisjoint(env)
    assert not any("ASANA" in name.upper() for name in env)
    assert not any(
        name.startswith("DISH_DARK_LAUNCH_") or "PROJECTION_ADAPTER" in name
        for name in env
    )


def test_production_postgresql_env_has_distinct_private_admin_and_action_credentials() -> None:
    env = _assignments(PRODUCTION_ENV)

    assert env["DISH_SERVICE_AGENT_TOKEN"]
    assert env["DISH_SERVICE_ADMIN_TOKEN"]
    assert env["DISH_SERVICE_ACTION_TOKEN"]
    assert len(
        {
            env["DISH_SERVICE_AGENT_TOKEN"],
            env["DISH_SERVICE_ADMIN_TOKEN"],
            env["DISH_SERVICE_ACTION_TOKEN"],
        }
    ) == 3
    assert env["DISH_SERVICE_BIND"] == "127.0.0.1"
    assert env["DISH_ACTION_BIND"] == "127.0.0.1"
