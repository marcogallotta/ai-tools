from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import runpy
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from dish_pg.production_reset import (
    ProductionResetError,
    load_recovery_record,
    read_reset_guard,
    recreate_database,
    snapshot_database_state,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]

ROOT = Path(__file__).resolve().parents[3]
RESET = ROOT / "scripts/dish-pg-production-reset"
RESET_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RESET_ID = "22222222-2222-4222-8222-222222222222"
RESET_POSTGRESQL_DSN_ENV = "DISH_TEST_POSTGRESQL_RESET_DSN"


def _q(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _native_reset_fixture(base):
    base_url = make_url(base.sqlalchemy_url)
    reset_dsn = os.environ.get(RESET_POSTGRESQL_DSN_ENV)
    if not reset_dsn:
        pytest.skip(
            f"requires {RESET_POSTGRESQL_DSN_ENV} for the disposable superuser reset owner"
        )
    reset_url = make_url(reset_dsn)
    if (reset_url.host, reset_url.port, reset_url.database) != (
        base_url.host,
        base_url.port,
        base_url.database,
    ):
        pytest.skip(
            f"requires {RESET_POSTGRESQL_DSN_ENV} to target the same isolated PostgreSQL database"
        )
    suffix = uuid.uuid4().hex[:8]
    database_name = f"dish_reset_{suffix}"
    observer = f"dish_observer_{suffix}"
    writer = f"dish_writer_{suffix}"
    admin_engine = create_engine(reset_url)
    target_engine = None
    try:
        with admin_engine.connect() as raw_connection:
            connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
            identity = connection.execute(
                text(
                    """
                    SELECT current_user AS role, rolsuper AS superuser
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
            ).mappings().one()
            owner = str(identity["role"])
            if not bool(identity["superuser"]):
                pytest.skip(
                    f"requires {RESET_POSTGRESQL_DSN_ENV} to connect as a PostgreSQL superuser"
                )
            connection.exec_driver_sql(f"CREATE ROLE {_q(observer)} NOLOGIN")
            connection.exec_driver_sql(f"CREATE ROLE {_q(writer)} NOLOGIN")
            connection.exec_driver_sql(
                f"CREATE DATABASE {_q(database_name)} OWNER {_q(owner)} TEMPLATE template0"
            )

        database_url = reset_url.set(database=database_name).render_as_string(
            hide_password=False
        )
        target_engine = create_engine(database_url)
        with target_engine.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA app")
            connection.exec_driver_sql(
                "CREATE TABLE app.items (id bigint PRIMARY KEY, payload text, private_note text)"
            )
            connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES ON DATABASE {_q(database_name)} FROM PUBLIC"
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {_q(database_name)} TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {_q(database_name)} TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA app TO {_q(observer)}, {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE app.items TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"GRANT UPDATE ON TABLE app.items TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT SELECT (private_note) ON TABLE app.items TO {_q(writer)}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE, SELECT ON SEQUENCE app.item_seq TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {_q(owner)} IN SCHEMA app "
                f"GRANT SELECT ON TABLES TO {_q(observer)}"
            )
            connection.exec_driver_sql(
                f"ALTER ROLE {_q(observer)} IN DATABASE {_q(database_name)} "
                "SET default_transaction_read_only TO 'on'"
            )
            connection.exec_driver_sql(
                f"ALTER DATABASE {_q(database_name)} SET lock_timeout TO '3s'"
            )

        yield {
            "database_name": database_name,
            "database_url": database_url,
            "observer": observer,
            "writer": writer,
            "owner": owner,
            "admin_engine": admin_engine,
        }
    finally:
        if target_engine is not None:
            target_engine.dispose()
        try:
            with admin_engine.connect() as raw_connection:
                connection = raw_connection.execution_options(isolation_level="AUTOCOMMIT")
                connection.exec_driver_sql(
                    f"DROP DATABASE IF EXISTS {_q(database_name)} WITH (FORCE)"
                )
                connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(observer)}")
                connection.exec_driver_sql(f"DROP ROLE IF EXISTS {_q(writer)}")
        finally:
            admin_engine.dispose()


def _assert_original_authority(
    database_url: str,
    *,
    database_name: str,
    observer: str,
    writer: str,
) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT has_database_privilege(:role, :database, 'CONNECT')"),
                {"role": observer, "database": database_name},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_schema_privilege(:role, 'app', 'USAGE')"),
                {"role": observer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_table_privilege(:role, 'app.items', 'SELECT')"),
                {"role": observer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_table_privilege(:role, 'app.items', 'UPDATE')"),
                {"role": writer},
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT has_column_privilege(:role, 'app.items', "
                    "'private_note', 'SELECT')"
                ),
                {"role": writer},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_sequence_privilege(:role, 'app.item_seq', 'USAGE')"),
                {"role": observer},
            ).scalar_one()
            settings = set(
                connection.execute(
                    text(
                        """
                        SELECT setting_role.rolname AS role_name, setting
                        FROM pg_db_role_setting AS role_setting
                        LEFT JOIN pg_roles AS setting_role
                          ON setting_role.oid = role_setting.setrole
                        CROSS JOIN LATERAL unnest(role_setting.setconfig) AS setting
                        WHERE role_setting.setdatabase = (
                            SELECT oid FROM pg_database WHERE datname = :database
                        )
                        """
                    ),
                    {"database": database_name},
                ).all()
            )
            assert (observer, "default_transaction_read_only=on") in settings
            assert (None, "lock_timeout=3s") in settings

            connection.exec_driver_sql(
                "CREATE TABLE app.future_items (id bigint PRIMARY KEY)"
            )
            assert connection.execute(
                text(
                    "SELECT has_table_privilege(:role, 'app.future_items', 'SELECT')"
                ),
                {"role": observer},
            ).scalar_one()
    finally:
        engine.dispose()


def test_native_partial_reset_retry_refuses_and_resume_restores_original_authority(
    native_migration_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _native_reset_fixture(native_migration_database) as fixture:
        database_url = fixture["database_url"]
        database_name = fixture["database_name"]
        observer = fixture["observer"]
        writer = fixture["writer"]
        recovery_path = tmp_path / "production-reset-recovery.json"

        namespace = runpy.run_path(str(RESET))
        main = namespace["main"]
        globals_ = main.__globals__
        monkeypatch.setenv("DISH_PG_DATABASE_URL", database_url)
        monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", database_name)
        monkeypatch.setenv("DISH_PG_CAPTURE_ENVIRONMENT", "production")

        prepare_calls: list[str] = []

        def fail_prepare(*, preflight_only: bool) -> None:
            prepare_calls.append("preflight" if preflight_only else "prepare")
            if not preflight_only:
                raise ProductionResetError("native injected prepare failure")

        monkeypatch.setitem(globals_, "_run_prepare", fail_prepare)
        args = [
            "--confirm-database-name",
            database_name,
            "--recovery-record",
            str(recovery_path),
        ]
        assert main(args) == 1
        assert prepare_calls == ["preflight", "prepare"]

        record = load_recovery_record(recovery_path)
        assert record.state == "reset_started"
        assert read_reset_guard(database_url, database_name) == record.reset_id
        assert any(
            grant.grantee == observer
            and grant.object_type == "TABLE"
            and grant.privilege == "SELECT"
            for grant in record.snapshot.object_grants
        )
        assert any(
            setting.role_name is None
            and setting.name == "lock_timeout"
            and setting.value == "3s"
            for setting in record.snapshot.settings
        )

        target_engine = create_engine(database_url)
        try:
            with target_engine.connect() as connection:
                assert not connection.execute(
                    text("SELECT has_database_privilege(:role, :database, 'CONNECT')"),
                    {"role": observer, "database": database_name},
                ).scalar_one()
        finally:
            target_engine.dispose()

        monkeypatch.setitem(
            globals_,
            "_run_prepare",
            lambda **_kwargs: pytest.fail("ordinary retry reached prepare"),
        )
        assert main(args) == 1
        assert load_recovery_record(recovery_path).state == "reset_started"
        assert read_reset_guard(database_url, database_name) == record.reset_id

        def successful_prepare(*, preflight_only: bool) -> None:
            if preflight_only:
                return
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql("CREATE SCHEMA app")
                    connection.exec_driver_sql(
                        "CREATE TABLE app.items ("
                        "id bigint PRIMARY KEY, payload text, private_note text)"
                    )
                    connection.exec_driver_sql("CREATE SEQUENCE app.item_seq")
            finally:
                engine.dispose()

        monkeypatch.setitem(globals_, "_run_prepare", successful_prepare)
        assert main([*args, "--resume"]) == 0
        completed = load_recovery_record(recovery_path)
        assert completed.state == "completed"
        assert completed.reset_id == record.reset_id
        assert completed.snapshot == record.snapshot
        assert read_reset_guard(database_url, database_name) is None

        _assert_original_authority(
            database_url,
            database_name=database_name,
            observer=observer,
            writer=writer,
        )


def test_native_create_before_guard_interruption_remains_connection_fenced(
    native_migration_database,
) -> None:
    with _native_reset_fixture(native_migration_database) as fixture:
        database_url = fixture["database_url"]
        database_name = fixture["database_name"]
        admin_engine = fixture["admin_engine"]
        snapshot = snapshot_database_state(database_url, database_name)

        class CrashBoundary(RuntimeError):
            pass

        def crash_after_create(message: str) -> None:
            if "database recreated with ALLOW_CONNECTIONS=false" in message:
                raise CrashBoundary("simulated process death before guard install")

        with pytest.raises(CrashBoundary, match="before guard install"):
            recreate_database(
                database_url,
                snapshot,
                reset_id=RESET_ID,
                log=crash_after_create,
            )

        with admin_engine.connect() as connection:
            allow_connections = connection.execute(
                text("SELECT datallowconn FROM pg_database WHERE datname = :database"),
                {"database": database_name},
            ).scalar_one()
        assert allow_connections is False
        assert read_reset_guard(database_url, database_name) is None


def test_native_guard_reset_id_mismatch_cannot_mutate_target(
    native_migration_database,
) -> None:
    with _native_reset_fixture(native_migration_database) as fixture:
        database_url = fixture["database_url"]
        database_name = fixture["database_name"]
        admin_engine = fixture["admin_engine"]
        snapshot = snapshot_database_state(database_url, database_name)

        recreate_database(database_url, snapshot, reset_id=RESET_ID)
        assert read_reset_guard(database_url, database_name) == RESET_ID
        with admin_engine.connect() as connection:
            before_oid = connection.execute(
                text("SELECT oid FROM pg_database WHERE datname = :database"),
                {"database": database_name},
            ).scalar_one()

        with pytest.raises(ProductionResetError, match="guard/reset-id mismatch"):
            recreate_database(
                database_url,
                snapshot,
                reset_id=OTHER_RESET_ID,
                resume=True,
            )

        with admin_engine.connect() as connection:
            after_oid, allow_connections = connection.execute(
                text(
                    "SELECT oid, datallowconn FROM pg_database "
                    "WHERE datname = :database"
                ),
                {"database": database_name},
            ).one()
        assert after_oid == before_oid
        assert allow_connections is True
        assert read_reset_guard(database_url, database_name) == RESET_ID
