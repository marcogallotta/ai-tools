from __future__ import annotations

import json
import os
import runpy
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from dish_pg import production_reset
from dish_pg.production_reset import (
    RESET_GUARD_SETTING,
    DatabaseDefinition,
    DatabaseSetting,
    DefaultGrant,
    DefaultPrivilegeSet,
    ObjectGrant,
    ProductionResetError,
    ResetSnapshot,
    ResetTargetIdentity,
    _database_create_sql,
    _grant_statement,
    _load_database_settings,
    create_recovery_record,
    load_recovery_record,
    maintenance_database_url,
    new_recovery_record,
    redacted_database_url,
    transition_recovery_record,
    validate_cli_target,
)

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "scripts/dish-pg-production-prepare"
RESET = ROOT / "scripts/dish-pg-production-reset"
RESET_ID = "11111111-1111-4111-8111-111111111111"
OTHER_RESET_ID = "22222222-2222-4222-8222-222222222222"


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


def _target() -> ResetTargetIdentity:
    return ResetTargetIdentity(
        database_name="dish_stage_a_prod",
        owner="dish",
        cluster_system_identifier="7461234567890123456",
    )


def _resolution(snapshot: ResetSnapshot | None = None):
    snapshot = snapshot or _snapshot()
    return production_reset._new_access_resolution(
        reset_id=RESET_ID,
        snapshot=snapshot,
        effective_grants=snapshot.object_grants,
        skipped_grants=(),
        replacement_sources=(),
    )


def _record(state: str = "snapshot_captured"):
    record = new_recovery_record(
        target=_target(), snapshot=_snapshot(), reset_id=RESET_ID
    )
    if state == "snapshot_captured":
        return record
    transitions = {
        "reset_started": ["reset_started"],
        "access_restored": ["reset_started", "access_restored"],
        "completed": ["reset_started", "access_restored", "completed"],
    }
    current = record
    for new_state in transitions[state]:
        current = production_reset._record_from_values(
            reset_id=current.reset_id,
            target=current.target,
            snapshot=current.snapshot,
            state=new_state,
        )
    return current


def _configure_cli(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "postgresql+psycopg://dish:secret@127.0.0.1:55433/dish_stage_a_prod"
    monkeypatch.setenv("DISH_PG_DATABASE_URL", url)
    monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", "dish_stage_a_prod")
    monkeypatch.setenv("DISH_PG_CAPTURE_ENVIRONMENT", "production")
    return url


def _args(path: Path, *, resume: bool = False) -> list[str]:
    args = [
        "--confirm-database-name",
        "dish_stage_a_prod",
        "--recovery-record",
        str(path),
    ]
    if resume:
        args.append("--resume")
    return args


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
    with pytest.raises(
        ProductionResetError, match="DISH_PG_CAPTURE_ENVIRONMENT=production or test"
    ):
        validate_cli_target(
            database_url=url,
            expected_database_name="dish_stage_a_prod",
            confirmed_database_name="dish_stage_a_prod",
            capture_environment="rehearsal",
        )
    with pytest.raises(ProductionResetError, match="ending in '_test'"):
        validate_cli_target(
            database_url="postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_test",
            expected_database_name="dish_stage_a_test",
            confirmed_database_name="dish_stage_a_test",
            capture_environment="production",
        )
    with pytest.raises(ProductionResetError, match="disposable dish_\\*_test"):
        validate_cli_target(
            database_url=url,
            expected_database_name="dish_stage_a_prod",
            confirmed_database_name="dish_stage_a_prod",
            capture_environment="test",
        )


def test_test_reset_target_gate_accepts_only_disposable_test_database() -> None:
    test_url = "postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_test"
    validate_cli_target(
        database_url=test_url,
        expected_database_name="dish_stage_a_test",
        confirmed_database_name="dish_stage_a_test",
        capture_environment="test",
    )

    with pytest.raises(ProductionResetError, match="disposable dish_\\*_test"):
        validate_cli_target(
            database_url="postgresql+psycopg://dish:secret@127.0.0.1:55432/dish_stage_a_production_test",
            expected_database_name="dish_stage_a_production_test",
            confirmed_database_name="dish_stage_a_production_test",
            capture_environment="test",
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


def test_create_and_grant_sql_quote_catalog_identifiers_and_create_fence() -> None:
    connection = _fake_connection()
    database = _snapshot().database
    create_sql = _database_create_sql(connection, database, allow_connections=False)
    assert 'CREATE DATABASE "dish_stage_a_prod"' in create_sql
    assert 'OWNER = "dish"' in create_sql
    assert "TEMPLATE = template0" in create_sql
    assert "LOCALE_PROVIDER = libc" in create_sql
    assert "LC_COLLATE = 'C.UTF-8'" in create_sql
    assert "ALLOW_CONNECTIONS = false" in create_sql

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

    monkeypatch.setattr(
        namespace["subprocess"], "run", lambda *args, **kwargs: Completed()
    )
    namespace["run_step"]("redaction probe", ["tool", "--database-url", url])

    output = capsys.readouterr().out
    assert "do-not-print" not in output
    assert "***" in output


def test_recovery_record_is_restrictive_checksum_bound_and_stateful(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reset-recovery.json"
    record = _record()
    create_recovery_record(path, record)

    assert path.stat().st_mode & 0o077 == 0
    raw = path.read_text(encoding="utf-8")
    assert "secret" not in raw
    assert load_recovery_record(path) == record

    started = transition_recovery_record(
        path,
        expected_reset_id=RESET_ID,
        expected_state="snapshot_captured",
        new_state="reset_started",
    )
    assert started.state == "reset_started"
    assert load_recovery_record(path) == started

    with pytest.raises(ProductionResetError, match="already exists"):
        create_recovery_record(path, record)


def test_recovery_record_rejects_checksum_version_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reset-recovery.json"
    create_recovery_record(path, _record())
    document = json.loads(path.read_text(encoding="utf-8"))

    document["checksum"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ProductionResetError, match="checksum mismatch"):
        load_recovery_record(path)

    create_path = tmp_path / "version.json"
    create_recovery_record(create_path, _record())
    versioned = json.loads(create_path.read_text(encoding="utf-8"))
    versioned["version"] = 99
    create_path.write_text(json.dumps(versioned), encoding="utf-8")
    os.chmod(create_path, 0o600)
    with pytest.raises(ProductionResetError, match="unsupported.*version"):
        load_recovery_record(create_path)

    mismatch_path = tmp_path / "identity.json"
    mismatch = new_recovery_record(
        target=replace(_target(), database_name="dish_other_prod"),
        snapshot=_snapshot(),
        reset_id=RESET_ID,
    )
    create_recovery_record(mismatch_path, mismatch)
    with pytest.raises(ProductionResetError, match="target identity does not match"):
        load_recovery_record(mismatch_path)


def test_v1_inflight_record_upgrades_without_resnapshot_and_rejects_resolution_tamper(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1-reset-recovery.json"
    snapshot = _snapshot()
    v1 = production_reset._record_from_values(
        reset_id=RESET_ID,
        target=_target(),
        snapshot=snapshot,
        state="reset_started",
        version=1,
    )
    create_recovery_record(path, v1)
    assert load_recovery_record(path).version == 1

    resolution = _resolution(snapshot)
    upgraded = production_reset.persist_access_resolution(
        path,
        expected_reset_id=RESET_ID,
        expected_state="reset_started",
        resolution=resolution,
    )
    assert upgraded.version == 2
    assert upgraded.snapshot == snapshot
    assert upgraded.access_resolution == resolution
    assert (
        production_reset.persist_access_resolution(
            path,
            expected_reset_id=RESET_ID,
            expected_state="reset_started",
            resolution=resolution,
        )
        == upgraded
    )

    reclassified = production_reset._new_access_resolution(
        reset_id=RESET_ID,
        snapshot=snapshot,
        effective_grants=(),
        skipped_grants=snapshot.object_grants,
        replacement_sources=(),
    )
    with pytest.raises(
        ProductionResetError,
        match="persisted ACL resolution changed unexpectedly",
    ):
        production_reset.persist_access_resolution(
            path,
            expected_reset_id=RESET_ID,
            expected_state="reset_started",
            resolution=reclassified,
        )

    document = json.loads(path.read_text(encoding="utf-8"))
    document["access_resolution"]["checksum"] = "0" * 64
    payload = {key: value for key, value in document.items() if key != "checksum"}
    document["checksum"] = production_reset.hashlib.sha256(
        production_reset._canonical_json_bytes(payload)
    ).hexdigest()
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(
        ProductionResetError, match="access resolution checksum mismatch"
    ):
        load_recovery_record(path)


def test_recovery_record_rejects_reserved_guard_contamination() -> None:
    database = _snapshot().database
    result = SimpleNamespace(
        mappings=lambda: [
            {
                "role_name": None,
                "setting": f"{RESET_GUARD_SETTING}={RESET_ID}",
            }
        ]
    )
    connection = SimpleNamespace(execute=lambda *_args, **_kwargs: result)
    with pytest.raises(ProductionResetError, match="reserved production-reset guard"):
        _load_database_settings(connection, database)


def test_lineage_gate_refuses_active_guard_unresolved_artifact_and_completed_reuse(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RESET))
    gate = namespace["_load_lineage_for_invocation"]
    path = tmp_path / "lineage.json"

    with pytest.raises(ProductionResetError, match="active production-reset guard"):
        gate(recovery_path=path, guard_reset_id=RESET_ID, resume=False)

    create_recovery_record(path, _record("reset_started"))
    with pytest.raises(ProductionResetError, match="ordinary retry is forbidden"):
        gate(recovery_path=path, guard_reset_id=RESET_ID, resume=False)

    path.unlink()
    create_recovery_record(path, _record("completed"))
    with pytest.raises(ProductionResetError, match="completed.*will not be reused"):
        gate(recovery_path=path, guard_reset_id=None, resume=False)
    with pytest.raises(ProductionResetError, match="already completed"):
        gate(recovery_path=path, guard_reset_id=None, resume=True)


def test_reset_entrypoint_orders_lineage_before_snapshot_and_finalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(RESET))
    url = _configure_cli(monkeypatch)
    recovery_path = tmp_path / "lineage.json"
    calls: list[str] = []
    snapshot = _snapshot()
    record = _record()

    main = namespace["main"]
    globals_ = main.__globals__
    monkeypatch.setitem(
        globals_, "read_reset_guard", lambda *_args: calls.append("guard") or None
    )
    monkeypatch.setitem(
        globals_,
        "_run_prepare",
        lambda *, preflight_only: calls.append(
            "preflight" if preflight_only else "prepare"
        ),
    )
    monkeypatch.setitem(
        globals_,
        "snapshot_database_state",
        lambda database_url, expected: calls.append("snapshot") or snapshot,
    )
    monkeypatch.setitem(
        globals_,
        "capture_reset_target_identity",
        lambda database_url, reset_snapshot: calls.append("identity") or _target(),
    )
    monkeypatch.setitem(globals_, "new_recovery_record", lambda **_kwargs: record)
    monkeypatch.setitem(
        globals_, "create_recovery_record", lambda *_args: calls.append("create-record")
    )

    states = iter(["reset_started", "access_restored", "completed"])

    def fake_transition(*_args, **kwargs):
        new_state = kwargs["new_state"]
        assert new_state == next(states)
        calls.append(f"state:{new_state}")
        return replace(record, state=new_state)

    monkeypatch.setitem(globals_, "transition_recovery_record", fake_transition)

    def fake_recreate(database_url, reset_snapshot, **kwargs):
        assert database_url == url
        assert reset_snapshot is snapshot
        assert kwargs["reset_id"] == RESET_ID
        assert kwargs["resume"] is False
        calls.append("recreate")

    monkeypatch.setitem(globals_, "recreate_database", fake_recreate)
    monkeypatch.setitem(
        globals_,
        "restore_database_access",
        lambda *_args, **_kwargs: calls.append("restore"),
    )
    monkeypatch.setitem(
        globals_, "clear_reset_guard", lambda *_args: calls.append("clear")
    )
    monkeypatch.setitem(
        globals_,
        "_ensure_access_resolution",
        lambda **_kwargs: (
            calls.append("resolve")
            or replace(record, access_resolution=_resolution(snapshot))
        ),
    )

    assert main(_args(recovery_path)) == 0
    assert calls == [
        "guard",
        "preflight",
        "snapshot",
        "identity",
        "create-record",
        "state:reset_started",
        "recreate",
        "prepare",
        "resolve",
        "restore",
        "state:access_restored",
        "clear",
        "state:completed",
    ]


def test_prepare_failure_retains_reset_started_lineage_and_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(RESET))
    _configure_cli(monkeypatch)
    recovery_path = tmp_path / "lineage.json"
    calls: list[str] = []
    snapshot = _snapshot()
    record = _record()

    main = namespace["main"]
    globals_ = main.__globals__
    monkeypatch.setitem(globals_, "read_reset_guard", lambda *_args: None)

    def fake_prepare(*, preflight_only: bool) -> None:
        calls.append("preflight" if preflight_only else "prepare")
        if not preflight_only:
            raise ProductionResetError("prepare failed")

    monkeypatch.setitem(globals_, "_run_prepare", fake_prepare)
    monkeypatch.setitem(globals_, "snapshot_database_state", lambda *_args: snapshot)
    monkeypatch.setitem(
        globals_, "capture_reset_target_identity", lambda *_args: _target()
    )
    monkeypatch.setitem(globals_, "new_recovery_record", lambda **_kwargs: record)
    monkeypatch.setitem(globals_, "create_recovery_record", lambda *_args: None)

    def fake_transition(*_args, **kwargs):
        calls.append(f"state:{kwargs['new_state']}")
        return replace(record, state=kwargs["new_state"])

    monkeypatch.setitem(globals_, "transition_recovery_record", fake_transition)
    monkeypatch.setitem(
        globals_,
        "recreate_database",
        lambda *_args, **_kwargs: calls.append("recreate"),
    )
    monkeypatch.setitem(
        globals_,
        "restore_database_access",
        lambda *_args, **_kwargs: calls.append("restore"),
    )
    monkeypatch.setitem(
        globals_, "clear_reset_guard", lambda *_args: calls.append("clear")
    )
    monkeypatch.setitem(
        globals_,
        "_ensure_access_resolution",
        lambda **_kwargs: (
            calls.append("resolve")
            or replace(record, access_resolution=_resolution(record.snapshot))
        ),
    )

    assert main(_args(recovery_path)) == 1
    assert calls == ["preflight", "state:reset_started", "recreate", "prepare"]


def test_explicit_resume_uses_retained_snapshot_and_never_snapshots_live_acl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = runpy.run_path(str(RESET))
    _configure_cli(monkeypatch)
    recovery_path = tmp_path / "lineage.json"
    recovery_path.touch(mode=0o600)
    record = _record("reset_started")
    calls: list[str] = []

    main = namespace["main"]
    globals_ = main.__globals__
    monkeypatch.setitem(globals_, "read_reset_guard", lambda *_args: RESET_ID)
    monkeypatch.setitem(globals_, "load_recovery_record", lambda *_args: record)
    monkeypatch.setitem(
        globals_,
        "validate_recovery_record_target",
        lambda *_args: calls.append("identity"),
    )
    monkeypatch.setitem(
        globals_,
        "snapshot_database_state",
        lambda *_args: pytest.fail("live snapshot on resume"),
    )
    monkeypatch.setitem(
        globals_,
        "_run_prepare",
        lambda *, preflight_only: calls.append(
            "preflight" if preflight_only else "prepare"
        ),
    )

    def fake_recreate(database_url, reset_snapshot, **kwargs):
        assert reset_snapshot is record.snapshot
        assert kwargs["resume"] is True
        assert kwargs["reset_id"] == RESET_ID
        calls.append("recreate")

    monkeypatch.setitem(globals_, "recreate_database", fake_recreate)
    monkeypatch.setitem(
        globals_,
        "restore_database_access",
        lambda *_args, **_kwargs: calls.append("restore"),
    )
    monkeypatch.setitem(
        globals_, "clear_reset_guard", lambda *_args: calls.append("clear")
    )
    monkeypatch.setitem(
        globals_,
        "_ensure_access_resolution",
        lambda **_kwargs: (
            calls.append("resolve")
            or replace(record, access_resolution=_resolution(record.snapshot))
        ),
    )

    transition_states = iter(["access_restored", "completed"])

    def fake_transition(*_args, **kwargs):
        assert kwargs["new_state"] == next(transition_states)
        calls.append(f"state:{kwargs['new_state']}")
        return replace(record, state=kwargs["new_state"])

    monkeypatch.setitem(globals_, "transition_recovery_record", fake_transition)

    assert main(_args(recovery_path, resume=True)) == 0
    assert calls == [
        "identity",
        "preflight",
        "recreate",
        "prepare",
        "resolve",
        "restore",
        "state:access_restored",
        "clear",
        "state:completed",
    ]


@pytest.mark.parametrize("guard_present", [True, False])
def test_resume_access_restored_covers_both_finalization_crash_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    guard_present: bool,
) -> None:
    namespace = runpy.run_path(str(RESET))
    _configure_cli(monkeypatch)
    recovery_path = tmp_path / "lineage.json"
    recovery_path.touch(mode=0o600)
    record = _record("access_restored")
    calls: list[str] = []

    main = namespace["main"]
    globals_ = main.__globals__
    guard_values = [RESET_ID if guard_present else None]
    guard_values.append(RESET_ID if guard_present else None)
    monkeypatch.setitem(
        globals_, "read_reset_guard", lambda *_args: guard_values.pop(0)
    )
    monkeypatch.setitem(globals_, "load_recovery_record", lambda *_args: record)
    monkeypatch.setitem(
        globals_,
        "validate_recovery_record_target",
        lambda *_args: calls.append("identity"),
    )
    monkeypatch.setitem(
        globals_,
        "_ensure_access_resolution",
        lambda **_kwargs: (
            calls.append("resolve")
            or replace(record, access_resolution=_resolution(record.snapshot))
        ),
    )
    monkeypatch.setitem(
        globals_, "verify_database_access", lambda *_args: calls.append("verify")
    )
    monkeypatch.setitem(
        globals_, "clear_reset_guard", lambda *_args: calls.append("clear")
    )
    monkeypatch.setitem(
        globals_,
        "_run_prepare",
        lambda **_kwargs: pytest.fail("prepare during finalization"),
    )
    monkeypatch.setitem(
        globals_,
        "recreate_database",
        lambda *_args, **_kwargs: pytest.fail("recreate during finalization"),
    )

    def fake_transition(*_args, **kwargs):
        assert kwargs["expected_state"] == "access_restored"
        assert kwargs["new_state"] == "completed"
        calls.append("state:completed")
        return replace(record, state="completed")

    monkeypatch.setitem(globals_, "transition_recovery_record", fake_transition)

    assert main(_args(recovery_path, resume=True)) == 0
    expected = ["identity", "resolve", "verify"]
    if guard_present:
        expected.append("clear")
    expected.append("state:completed")
    assert calls == expected


def test_recreate_orders_create_fence_guard_and_non_owner_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    sql: list[str] = []

    class FakeConnection:
        dialect = postgresql.dialect()

        def execution_options(self, **_kwargs):
            return self

        def exec_driver_sql(self, statement: str):
            sql.append(statement)

    connection = FakeConnection()

    class ConnectContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    class FakeEngine:
        def connect(self):
            return ConnectContext()

        def dispose(self):
            pass

    guard_reads = iter([None, RESET_ID])
    monkeypatch.setattr(
        production_reset, "create_engine", lambda *_args, **_kwargs: FakeEngine()
    )
    monkeypatch.setattr(production_reset, "_database_exists", lambda *_args: True)
    monkeypatch.setattr(
        production_reset, "_load_database_definition", lambda *_args: snapshot.database
    )
    monkeypatch.setattr(
        production_reset, "_maintenance_actor_identity", lambda *_args: ("dish", True)
    )
    monkeypatch.setattr(
        production_reset, "_load_reset_guard", lambda *_args: next(guard_reads)
    )
    monkeypatch.setattr(
        production_reset, "_check_non_session_drop_blockers", lambda *_args: None
    )
    monkeypatch.setattr(production_reset, "_active_sessions", lambda *_args: [])

    production_reset.recreate_database(
        "postgresql+psycopg://dish:secret@localhost/dish_stage_a_prod",
        snapshot,
        reset_id=RESET_ID,
    )

    assert "ALLOW_CONNECTIONS false" in sql[0]
    assert sql[1].startswith('DROP DATABASE "dish_stage_a_prod"')
    assert sql[2].startswith('CREATE DATABASE "dish_stage_a_prod"')
    assert "ALLOW_CONNECTIONS = false" in sql[2]
    assert f"SET {RESET_GUARD_SETTING}" in sql[3]
    assert sql[4].startswith("REVOKE ALL PRIVILEGES ON DATABASE")
    assert "ALLOW_CONNECTIONS true" in sql[5]


def test_guard_reset_id_mismatch_refuses_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    sql: list[str] = []

    class FakeConnection:
        dialect = postgresql.dialect()

        def execution_options(self, **_kwargs):
            return self

        def exec_driver_sql(self, statement: str):
            sql.append(statement)

    connection = FakeConnection()

    @contextmanager
    def connected():
        yield connection

    engine = SimpleNamespace(connect=connected, dispose=lambda: None)
    monkeypatch.setattr(
        production_reset, "create_engine", lambda *_args, **_kwargs: engine
    )
    monkeypatch.setattr(production_reset, "_database_exists", lambda *_args: True)
    monkeypatch.setattr(
        production_reset, "_load_database_definition", lambda *_args: snapshot.database
    )
    monkeypatch.setattr(
        production_reset, "_maintenance_actor_identity", lambda *_args: ("dish", True)
    )
    monkeypatch.setattr(
        production_reset, "_load_reset_guard", lambda *_args: OTHER_RESET_ID
    )

    with pytest.raises(ProductionResetError, match="guard/reset-id mismatch"):
        production_reset.recreate_database(
            "postgresql+psycopg://dish:secret@localhost/dish_stage_a_prod",
            snapshot,
            reset_id=RESET_ID,
            resume=True,
        )
    assert sql == []
