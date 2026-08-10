from __future__ import annotations

from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from dish_pg.production_reset import (
    DatabaseDefinition,
    DatabaseSetting,
    DefaultGrant,
    DefaultPrivilegeSet,
    ObjectGrant,
    ProductionResetError,
    ResetSnapshot,
    _database_create_sql,
    _grant_statement,
    maintenance_database_url,
    redacted_database_url,
    validate_cli_target,
)


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "scripts/dish-pg-production-prepare"
RESET = ROOT / "scripts/dish-pg-production-reset"


def _fake_connection():
    return SimpleNamespace(dialect=postgresql.dialect())


def _snapshot() -> ResetSnapshot:
    return ResetSnapshot(
        database=DatabaseDefinition(
            name="dish_stage_a_prod",
            owner="dish",
            encoding="UTF8",
            locale_provider="libc",
            lc_collate="C.UTF-8",
            lc_ctype="C.UTF-8",
            locale=None,
            icu_rules=None,
            tablespace="pg_default",
            connection_limit=-1,
            allow_connections=True,
            is_template=False,
        ),
        object_grants=(
            ObjectGrant(
                object_type="TABLE",
                schema_name="public",
                object_name="tasks",
                column_name=None,
                grantee="dish_frontend_observer",
                privilege="SELECT",
                grantable=False,
            ),
        ),
        settings=(
            DatabaseSetting(
                role_name="dish_frontend_observer",
                name="default_transaction_read_only",
                value="on",
            ),
        ),
        default_privileges=(
            DefaultPrivilegeSet(
                owner="dish",
                schema_name="public",
                object_type="TABLES",
                grants=(
                    DefaultGrant(
                        grantee="dish_frontend_observer",
                        privilege="SELECT",
                        grantable=False,
                    ),
                ),
            ),
        ),
    )


def test_production_reset_target_gate_is_explicit_and_fail_closed() -> None:
    url = "postgresql+psycopg://dish:secret@127.0.0.1:55433/dish_stage_a_prod"
    validate_cli_target(
        database_url=url,
        expected_database_name="dish_stage_a_prod",
        confirmed_database_name="dish_stage_a_prod",
        capture_environment="production",
    )

    with pytest.raises(ProductionResetError, match="confirm-database-name"):
        validate_cli_target(
            database_url=url,
            expected_database_name="dish_stage_a_prod",
            confirmed_database_name="dish_other_prod",
            capture_environment="production",
        )
    with pytest.raises(ProductionResetError, match="DISH_PG_CAPTURE_ENVIRONMENT=production"):
        validate_cli_target(
            database_url=url,
            expected_database_name="dish_stage_a_prod",
            confirmed_database_name="dish_stage_a_prod",
            capture_environment="test",
        )
    with pytest.raises(ProductionResetError, match="ending in '_test'"):
        validate_cli_target(
            database_url="postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_test",
            expected_database_name="dish_stage_a_test",
            confirmed_database_name="dish_stage_a_test",
            capture_environment="production",
        )



def test_reset_entrypoint_requires_explicit_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(RESET))
    monkeypatch.setenv(
        "DISH_PG_DATABASE_URL",
        "postgresql+psycopg://dish:secret@127.0.0.1:55433/dish_stage_a_prod",
    )
    monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", "dish_stage_a_prod")
    monkeypatch.delenv("DISH_PG_CAPTURE_ENVIRONMENT", raising=False)

    with pytest.raises(ProductionResetError, match="DISH_PG_CAPTURE_ENVIRONMENT"):
        namespace["_required_environment"]()

def test_database_url_helpers_preserve_target_shape_without_exposing_password() -> None:
    url = (
        "postgresql+psycopg://dish:super-secret@127.0.0.1:55433/"
        "dish_stage_a_prod?sslmode=disable"
    )
    maintenance = maintenance_database_url(url)
    assert "super-secret" in maintenance
    assert "/postgres?" in maintenance
    assert "sslmode=disable" in maintenance

    redacted = redacted_database_url(url)
    assert "super-secret" not in redacted
    assert "***" in redacted
    assert "dish_stage_a_prod" in redacted


def test_create_and_grant_sql_quote_catalog_identifiers() -> None:
    connection = _fake_connection()
    database = _snapshot().database
    create_sql = _database_create_sql(connection, database)
    assert 'CREATE DATABASE "dish_stage_a_prod"' in create_sql
    assert 'OWNER = "dish"' in create_sql
    assert "TEMPLATE = template0" in create_sql
    assert "LOCALE_PROVIDER = libc" in create_sql
    assert "LC_COLLATE = 'C.UTF-8'" in create_sql

    grant = ObjectGrant(
        object_type="COLUMN",
        schema_name='odd"schema',
        object_name='odd"table',
        column_name='odd"column',
        grantee='odd"role',
        privilege="SELECT",
        grantable=True,
    )
    statement = _grant_statement(connection, grant)
    assert '"odd""schema"."odd""table"' in statement
    assert '("odd""column")' in statement
    assert 'TO "odd""role" WITH GRANT OPTION' in statement


def test_prepare_command_logging_redacts_database_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    namespace = runpy.run_path(str(PREPARE))
    url = "postgresql+psycopg://dish:do-not-print@127.0.0.1:55433/dish_stage_a_prod"
    monkeypatch.setenv("DISH_PG_DATABASE_URL", url)

    class Completed:
        returncode = 0
        stdout = f"child echoed {url}\n"
        stderr = ""

    monkeypatch.setattr(namespace["subprocess"], "run", lambda *args, **kwargs: Completed())
    namespace["run_step"]("redaction probe", ["tool", "--database-url", url])

    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert "***" in output


def test_reset_entrypoint_sequences_preflight_reset_prepare_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(RESET))
    url = "postgresql+psycopg://dish:secret@127.0.0.1:55433/dish_stage_a_prod"
    monkeypatch.setenv("DISH_PG_DATABASE_URL", url)
    monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", "dish_stage_a_prod")
    monkeypatch.setenv("DISH_PG_CAPTURE_ENVIRONMENT", "production")

    calls: list[str] = []
    snapshot = _snapshot()

    def fake_prepare(*, preflight_only: bool) -> None:
        calls.append("preflight" if preflight_only else "prepare")

    def fake_snapshot(database_url: str, expected_database_name: str) -> ResetSnapshot:
        assert database_url == url
        assert expected_database_name == "dish_stage_a_prod"
        calls.append("snapshot")
        return snapshot

    def fake_recreate(database_url: str, reset_snapshot: ResetSnapshot, *, log) -> None:
        assert database_url == url
        assert reset_snapshot is snapshot
        calls.append("recreate")

    def fake_restore(database_url: str, reset_snapshot: ResetSnapshot) -> None:
        assert database_url == url
        assert reset_snapshot is snapshot
        calls.append("restore")

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_prepare", fake_prepare)
    monkeypatch.setitem(main.__globals__, "snapshot_database_state", fake_snapshot)
    monkeypatch.setitem(main.__globals__, "recreate_database", fake_recreate)
    monkeypatch.setitem(main.__globals__, "restore_database_access", fake_restore)

    assert main(["--confirm-database-name", "dish_stage_a_prod"]) == 0
    assert calls == ["preflight", "snapshot", "recreate", "prepare", "restore"]


def test_reset_entrypoint_keeps_access_fenced_when_prepare_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(RESET))
    url = "postgresql+psycopg://dish:secret@127.0.0.1:55433/dish_stage_a_prod"
    monkeypatch.setenv("DISH_PG_DATABASE_URL", url)
    monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", "dish_stage_a_prod")
    monkeypatch.setenv("DISH_PG_CAPTURE_ENVIRONMENT", "production")

    calls: list[str] = []
    snapshot = _snapshot()

    def fake_prepare(*, preflight_only: bool) -> None:
        calls.append("preflight" if preflight_only else "prepare")
        if not preflight_only:
            raise ProductionResetError("prepare failed")

    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "_run_prepare", fake_prepare)
    monkeypatch.setitem(main.__globals__, "snapshot_database_state", lambda *_args: snapshot)
    monkeypatch.setitem(
        main.__globals__,
        "recreate_database",
        lambda *_args, **_kwargs: calls.append("recreate"),
    )
    monkeypatch.setitem(
        main.__globals__,
        "restore_database_access",
        lambda *_args: calls.append("restore"),
    )

    assert main(["--confirm-database-name", "dish_stage_a_prod"]) == 1
    assert calls == ["preflight", "recreate", "prepare"]
