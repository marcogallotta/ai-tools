from __future__ import annotations

import os
import runpy
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from dish_pg import production_reset
from dish_pg.frontend_board_query import (
    BoardContext,
    BoardRegistryFacts,
    FrontendBoardQuery,
)
from dish_pg.production_reset import (
    ObjectGrant,
    ProductionResetError,
    derive_access_resolution,
    load_recovery_record,
    read_reset_guard,
    recreate_database,
    restore_database_access,
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
            identity = (
                connection.execute(
                    text(
                        """
                    SELECT current_user AS role, rolsuper AS superuser
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                    )
                )
                .mappings()
                .one()
            )
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
                connection = raw_connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
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
                text("SELECT has_table_privilege(:role, 'app.future_items', 'SELECT')"),
                {"role": observer},
            ).scalar_one()
    finally:
        engine.dispose()


def test_native_concurrent_reset_contender_fails_before_lineage_or_destructive_mutation(
    native_migration_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _native_reset_fixture(native_migration_database) as fixture:
        database_url = fixture["database_url"]
        database_name = fixture["database_name"]
        admin_engine = fixture["admin_engine"]
        contender_recovery = tmp_path / "contender-reset.json"

        namespace = runpy.run_path(str(RESET))
        main = namespace["main"]
        hold_reset_target_lock = namespace["hold_reset_target_lock"]
        globals_ = main.__globals__
        monkeypatch.setenv("DISH_PG_DATABASE_URL", database_url)
        monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", database_name)
        monkeypatch.setenv("DISH_PG_CAPTURE_ENVIRONMENT", "production")
        monkeypatch.setitem(
            globals_,
            "_run_prepare",
            lambda **_kwargs: pytest.fail(
                "contending reset reached prepare while target lock was owned"
            ),
        )

        with admin_engine.connect() as connection:
            before_oid, before_allow_connections = connection.execute(
                text(
                    "SELECT oid, datallowconn FROM pg_database "
                    "WHERE datname = :database"
                ),
                {"database": database_name},
            ).one()

        with hold_reset_target_lock(database_url, database_name):
            assert (
                main(
                    [
                        "--confirm-database-name",
                        database_name,
                        "--recovery-record",
                        str(contender_recovery),
                    ]
                )
                == 1
            )

            assert not contender_recovery.exists()
            assert read_reset_guard(database_url, database_name) is None
            with admin_engine.connect() as connection:
                after_oid, after_allow_connections = connection.execute(
                    text(
                        "SELECT oid, datallowconn FROM pg_database "
                        "WHERE datname = :database"
                    ),
                    {"database": database_name},
                ).one()

        assert after_oid == before_oid
        assert before_allow_connections is True
        assert after_allow_connections is True


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
                        "CREATE TABLE alembic_version ("
                        "version_num varchar(32) NOT NULL PRIMARY KEY)"
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO alembic_version (version_num) "
                        "VALUES ('0044_independent_archive')"
                    )
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


def test_native_access_restore_rejects_unexpected_state_and_keeps_guard(
    native_migration_database,
) -> None:
    cases = (
        ("grant", "unexpected grant"),
        ("setting", "unexpected setting"),
        ("default_privilege", "unexpected definition"),
    )
    for extra_kind, error_match in cases:
        with _native_reset_fixture(native_migration_database) as fixture:
            database_url = fixture["database_url"]
            database_name = fixture["database_name"]
            observer = fixture["observer"]
            writer = fixture["writer"]
            owner = fixture["owner"]
            snapshot = snapshot_database_state(database_url, database_name)

            with fixture["admin_engine"].connect() as raw_connection:
                connection = raw_connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                connection.exec_driver_sql(
                    f"ALTER DATABASE {_q(database_name)} "
                    f"SET {production_reset.RESET_GUARD_SETTING} TO '{RESET_ID}'"
                )

            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    if extra_kind == "grant":
                        connection.exec_driver_sql(
                            f"GRANT INSERT ON TABLE app.items TO {_q(observer)}"
                        )
                    elif extra_kind == "setting":
                        connection.exec_driver_sql(
                            f"ALTER ROLE {_q(writer)} IN DATABASE {_q(database_name)} "
                            "SET statement_timeout TO '5s'"
                        )
                    else:
                        connection.exec_driver_sql(
                            f"ALTER DEFAULT PRIVILEGES FOR ROLE {_q(owner)} IN SCHEMA app "
                            f"GRANT INSERT ON TABLES TO {_q(writer)}"
                        )

                with pytest.raises(ProductionResetError, match=error_match):
                    restore_database_access(
                        database_url,
                        snapshot,
                        reset_id=RESET_ID,
                    )

                assert read_reset_guard(database_url, database_name) == RESET_ID
            finally:
                engine.dispose()


def test_native_0041_to_0044_acl_resolution_restores_real_observer_query(
    native_migration_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _native_reset_fixture(native_migration_database) as fixture:
        database_url = fixture["database_url"]
        database_name = fixture["database_name"]
        observer = fixture["observer"]
        writer = fixture["writer"]
        owner = fixture["owner"]
        engine = create_engine(database_url)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", database_url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA public CASCADE")
                connection.exec_driver_sql("CREATE SCHEMA public")
            command.upgrade(config, "0041_test_generation_rollover")
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA public TO {_q(observer)}"
                )
                connection.exec_driver_sql(
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_q(observer)}"
                )
            snapshot = snapshot_database_state(database_url, database_name)

            command.upgrade(config, "0044_independent_archive")
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"REVOKE SELECT ON TABLE public.dish_tasks FROM {_q(observer)}"
                )
            with fixture["admin_engine"].connect() as raw_connection:
                connection = raw_connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                connection.exec_driver_sql(
                    f"ALTER DATABASE {_q(database_name)} "
                    f"SET dish.production_reset_incomplete TO '{RESET_ID}'"
                )

            monkeypatch.setattr(production_reset, "_OBSERVER_ROLE", observer)
            replacement = production_reset.ObjectGrant(
                object_type="TABLE",
                schema_name="public",
                object_name="dish_states",
                column_name=None,
                grantee=observer,
                privilege="SELECT",
                grantable=False,
            )
            monkeypatch.setattr(
                production_reset, "_OBSERVER_REPLACEMENT_TARGET", replacement
            )

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE public.current_task_completion (id bigint)"
                )
            with pytest.raises(ProductionResetError, match="schema drift"):
                derive_access_resolution(database_url, snapshot, reset_id=RESET_ID)
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE public.current_task_completion")

            partial_snapshot = replace(
                snapshot,
                object_grants=tuple(
                    grant
                    for grant in snapshot.object_grants
                    if not (
                        grant.grantee == observer
                        and grant.object_name == "task_completion_events"
                    )
                ),
            )
            with pytest.raises(ProductionResetError, match="exact six"):
                derive_access_resolution(
                    database_url, partial_snapshot, reset_id=RESET_ID
                )

            mismatched_observer_snapshot = replace(
                snapshot,
                object_grants=tuple(
                    replace(grant, grantable=True)
                    if grant.grantee == observer
                    and grant.object_name == "task_completion_events"
                    else grant
                    for grant in snapshot.object_grants
                ),
            )
            with pytest.raises(ProductionResetError, match="exact six"):
                derive_access_resolution(
                    database_url,
                    mismatched_observer_snapshot,
                    reset_id=RESET_ID,
                )

            missing_relation = ObjectGrant(
                object_type="TABLE",
                schema_name="public",
                object_name="not_reviewed_as_retired",
                column_name=None,
                grantee=observer,
                privilege="SELECT",
                grantable=False,
            )
            with pytest.raises(ProductionResetError, match="not reviewed as retired"):
                derive_access_resolution(
                    database_url,
                    replace(
                        snapshot,
                        object_grants=(*snapshot.object_grants, missing_relation),
                    ),
                    reset_id=RESET_ID,
                )

            missing_role = replace(missing_relation, grantee="absent_reset_role")
            with pytest.raises(ProductionResetError, match=r"role\(s\) are missing"):
                derive_access_resolution(
                    database_url,
                    replace(
                        snapshot,
                        object_grants=(*snapshot.object_grants, missing_role),
                    ),
                    reset_id=RESET_ID,
                )

            missing_column = replace(
                missing_relation,
                object_type="COLUMN",
                object_name="dish_tasks",
                column_name="retired_column",
            )
            with pytest.raises(ProductionResetError, match="missing from a surviving"):
                derive_access_resolution(
                    database_url,
                    replace(
                        snapshot,
                        object_grants=(*snapshot.object_grants, missing_column),
                    ),
                    reset_id=RESET_ID,
                )

            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE SEQUENCE public.kind_mismatch")
            wrong_kind = replace(missing_relation, object_name="kind_mismatch")
            with pytest.raises(ProductionResetError, match="type changed"):
                derive_access_resolution(
                    database_url,
                    replace(
                        snapshot,
                        object_grants=(*snapshot.object_grants, wrong_kind),
                    ),
                    reset_id=RESET_ID,
                )
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SEQUENCE public.kind_mismatch")

            missing_schema = replace(
                missing_relation,
                object_type="SCHEMA",
                schema_name="absent_schema",
                object_name="absent_schema",
            )
            with pytest.raises(ProductionResetError, match="target schema is missing"):
                derive_access_resolution(
                    database_url,
                    replace(
                        snapshot,
                        object_grants=(*snapshot.object_grants, missing_schema),
                    ),
                    reset_id=RESET_ID,
                )

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"GRANT UPDATE ON TABLE public.dish_states TO {_q(observer)}"
                )
            with pytest.raises(ProductionResetError, match="incompatible existing ACL"):
                derive_access_resolution(database_url, snapshot, reset_id=RESET_ID)
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"REVOKE UPDATE ON TABLE public.dish_states FROM {_q(observer)}"
                )

            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE public.dish_states RENAME TO dish_states_missing"
                )
            with pytest.raises(ProductionResetError, match="not reviewed as retired"):
                derive_access_resolution(database_url, snapshot, reset_id=RESET_ID)
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE public.dish_states_missing RENAME TO dish_states"
                )

            snapshot_with_retired_column = replace(
                snapshot,
                object_grants=(
                    *snapshot.object_grants,
                    ObjectGrant(
                        object_type="COLUMN",
                        schema_name="public",
                        object_name="task_completion_events",
                        column_name="completed_at",
                        grantee=writer,
                        privilege="SELECT",
                        grantable=False,
                    ),
                ),
            )
            resolution = derive_access_resolution(
                database_url,
                snapshot_with_retired_column,
                reset_id=RESET_ID,
            )
            assert {
                (grant.schema_name, grant.object_name)
                for entry in resolution.skipped_grants
                for grant in (entry.source,)
            } == production_reset._RETIRED_0042_RELATIONS
            assert all(
                entry.reset_id == RESET_ID
                and entry.schema_head == "0044_independent_archive"
                and entry.target is None
                and entry.disposition == "retired_relation_skipped"
                for entry in resolution.skipped_grants
            )
            assert {entry.source for entry in resolution.replacements} == {
                ObjectGrant(
                    object_type="TABLE",
                    schema_name=schema_name,
                    object_name=relation_name,
                    column_name=None,
                    grantee=observer,
                    privilege="SELECT",
                    grantable=False,
                )
                for schema_name, relation_name in production_reset._RETIRED_0042_RELATIONS
            }
            assert all(
                entry.target == replacement
                and entry.reset_id == RESET_ID
                and entry.schema_head == "0044_independent_archive"
                and entry.disposition == "retired_observer_grant_replaced"
                for entry in resolution.replacements
            )

            recovery_path = tmp_path / "v1-acl-resolution.json"
            v1_record = production_reset._record_from_values(
                reset_id=RESET_ID,
                target=production_reset.ResetTargetIdentity(
                    database_name=database_name,
                    owner=owner,
                    cluster_system_identifier="native-certification",
                ),
                snapshot=snapshot_with_retired_column,
                state="reset_started",
                version=1,
            )
            production_reset.create_recovery_record(recovery_path, v1_record)
            durable_record = production_reset.persist_access_resolution(
                recovery_path,
                expected_reset_id=RESET_ID,
                expected_state="reset_started",
                resolution=resolution,
            )
            assert durable_record.version == 2
            assert durable_record.snapshot == snapshot_with_retired_column
            assert durable_record.access_resolution == resolution
            assert (
                production_reset.load_recovery_record(recovery_path).access_resolution
                == resolution
            )

            tampered_resolution = production_reset._new_access_resolution(
                reset_id=RESET_ID,
                snapshot=snapshot_with_retired_column,
                effective_grants=resolution.effective_grants,
                skipped_grants=tuple(
                    entry.source
                    for entry in resolution.skipped_grants
                    if entry.source.object_type != "COLUMN"
                ),
                replacement_sources=tuple(
                    entry.source for entry in resolution.replacements
                ),
            )
            with pytest.raises(ProductionResetError, match="no longer matches"):
                production_reset.validate_access_resolution(
                    database_url,
                    snapshot_with_retired_column,
                    tampered_resolution,
                )

            original_grant_statement = production_reset._grant_statement
            saw_survivor = False

            def fail_after_survivor(connection, grant):
                nonlocal saw_survivor
                if saw_survivor:
                    return "THIS IS NOT SQL"
                statement = original_grant_statement(connection, grant)
                if (
                    grant.grantee == observer
                    and grant.object_name == "dish_tasks"
                    and grant.privilege == "SELECT"
                ):
                    saw_survivor = True
                return statement

            monkeypatch.setattr(
                production_reset, "_grant_statement", fail_after_survivor
            )
            with pytest.raises(DBAPIError):
                restore_database_access(
                    database_url,
                    snapshot_with_retired_column,
                    reset_id=RESET_ID,
                    resolution=resolution,
                )
            with engine.connect() as connection:
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege(:role, "
                        "'public.dish_tasks', 'SELECT')"
                    ),
                    {"role": observer},
                ).scalar_one()
            monkeypatch.setattr(
                production_reset, "_grant_statement", original_grant_statement
            )

            restore_database_access(
                database_url,
                snapshot_with_retired_column,
                reset_id=RESET_ID,
                resolution=resolution,
            )
            assert (
                derive_access_resolution(
                    database_url,
                    snapshot_with_retired_column,
                    reset_id=RESET_ID,
                )
                == resolution
            )
            restore_database_access(
                database_url,
                snapshot_with_retired_column,
                reset_id=RESET_ID,
                resolution=resolution,
            )
            with engine.begin() as connection:
                connection.exec_driver_sql(f"SET LOCAL ROLE {_q(observer)}")
                session = Session(bind=connection)
                context = BoardContext(
                    generation_id=uuid.uuid4(),
                    registry_version_id=uuid.uuid4(),
                    registry_revision=1,
                    evaluation_time=datetime.now(timezone.utc),
                )
                registry = BoardRegistryFacts(context=context, sections=())
                assert (
                    FrontendBoardQuery(session).active_cards(
                        registry=registry,
                        projection_delay=timedelta(seconds=1),
                        max_cards=1,
                    )
                    == ()
                )
                with pytest.raises(DBAPIError):
                    connection.exec_driver_sql(
                        "INSERT INTO public.dish_states DEFAULT VALUES"
                    )
        finally:
            engine.dispose()
