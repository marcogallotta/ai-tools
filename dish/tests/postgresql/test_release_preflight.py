from __future__ import annotations

import pytest

from dish_pg import release_preflight
from dish_pg.schema_identity import ALEMBIC_HEAD


def test_configuration_schema_mismatch_fails_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(_url: str) -> str:
        raise AssertionError("database must not be accessed")

    monkeypatch.setattr(release_preflight, "check_migration_head", unexpected)
    with pytest.raises(
        release_preflight.ReleasePreflightError, match="candidate release"
    ):
        release_preflight.check_release_preflight(
            database_url="postgresql://invalid",
            expected_database="dish_prod",
            expected_schema_head="0045_old",
            expected_release="dish@generation",
            expected_generation_id="generation-id",
        )


def test_main_requires_complete_generation_identity_environment() -> None:
    assert (
        release_preflight.main({"DISH_PG_DATABASE_URL": "postgresql://invalid"}) == 78
    )


def test_main_passes_all_expected_identity_fields_to_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str] = {}

    def fake_check(**values: str) -> dict[str, str]:
        observed.update(values)
        return {"database": values["expected_database"]}

    monkeypatch.setattr(release_preflight, "check_release_preflight", fake_check)
    env = {
        "DISH_PG_DATABASE_URL": "postgresql://dish:secret@localhost/dish_prod",
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_prod",
        "DISH_PG_EXPECTED_SCHEMA_HEAD": ALEMBIC_HEAD,
        "DISH_PG_EXPECTED_RELEASE": "dish@generation",
        "DISH_PG_EXPECTED_GENERATION_ID": "11111111-1111-4111-8111-111111111111",
    }
    assert release_preflight.main(env) == 0
    assert observed == {
        "database_url": env["DISH_PG_DATABASE_URL"],
        "expected_database": env["DISH_PG_EXPECTED_DATABASE_NAME"],
        "expected_schema_head": env["DISH_PG_EXPECTED_SCHEMA_HEAD"],
        "expected_release": env["DISH_PG_EXPECTED_RELEASE"],
        "expected_generation_id": env["DISH_PG_EXPECTED_GENERATION_ID"],
    }
