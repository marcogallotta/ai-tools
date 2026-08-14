from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from dish_pg.production_reset import (
    RECOVERY_RECORD_FORMAT,
    RECOVERY_STATE_ACCESS_RESTORED,
    RECOVERY_STATE_COMPLETED,
    RECOVERY_STATE_DATABASE_RECREATED,
    RECOVERY_STATE_GUARD_INSTALLED,
    RECOVERY_STATE_PREPARE_FAILED,
    RECOVERY_STATE_SNAPSHOT_CAPTURED,
    RESET_GUARD_SETTING,
    DatabaseDefinition,
    DatabaseSetting,
    DefaultGrant,
    DefaultPrivilegeSet,
    ObjectGrant,
    ProductionResetError,
    ResetGuardState,
    ResetRecoveryRecord,
    ResetRecoveryStore,
    ResetSnapshot,
    _database_create_sql,
    _grant_statement,
    maintenance_database_url,
    parse_recovery_record,
    redacted_database_url,
    serialize_recovery_record,
    validate_cli_target,
)

ROOT = Path(__file__).resolve().parents[2]
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


def _record(state: str = RECOVERY_STATE_SNAPSHOT_CAPTURED) -> ResetRecoveryRecord:
    return ResetRecoveryRecord(
        reset_id="11111111-1111-4111-8111-111111111111",
        target_database="dish_stage_a_prod",
        owner="dish",
        cluster_system_identifier="7654321",
        state=state,
        snapshot=_snapshot(),
    )


def _store(tmp_path: Path, record: ResetRecoveryRecord | None = None) -> ResetRecoveryStore:
    path = tmp_path / "state" / "recovery.json"
    path.parent.mkdir(mode=0o700)
    store = ResetRecoveryStore(path)
    if record is not None:
        store.create(record)
    return store


def _canonical_checksum(payload: dict[str, object]) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "checksum_sha256"}
    return hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


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


def test_database_url_helpers_redact_password() -> None:
    url = "postgresql+psycopg://dish:super-secret@127.0.0.1:55433/dish_stage_a_prod?sslmode=disable"
    assert "super-secret" in maintenance_database_url(url)
    assert "/postgres?" in maintenance_database_url(url)
    assert "super-secret" not in redacted_database_url(url)


def test_create_sql_starts_recreated_database_connection_fenced() -> None:
    sql = _database_create_sql(_fake_connection(), _snapshot().database)
    assert 'CREATE DATABASE "dish_stage_a_prod"' in sql
    assert "ALLOW_CONNECTIONS = false" in sql


def test_grant_sql_quotes_catalog_identifiers() -> None:
    statement = _grant_statement(
        _fake_connection(),
        ObjectGrant("COLUMN", 'odd"schema', 'odd"table', 'odd"column', 'odd"role', "SELECT", True),
    )
    assert '"odd""schema"."odd""table"' in statement
    assert '("odd""column")' in statement
    assert 'TO "odd""role" WITH GRANT OPTION' in statement


def test_recovery_record_round_trips_original_snapshot_without_credentials() -> None:
    data = serialize_recovery_record(_record())
    assert b"secret" not in data
    assert b"postgresql" not in data
    assert parse_recovery_record(data) == _record()
    payload = json.loads(data)
    assert payload["format"] == RECOVERY_RECORD_FORMAT


def test_recovery_record_checksum_is_fail_closed() -> None:
    payload = json.loads(serialize_recovery_record(_record()))
    payload["state"] = RECOVERY_STATE_GUARD_INSTALLED
    with pytest.raises(ProductionResetError, match="checksum mismatch"):
        parse_recovery_record((json.dumps(payload) + "\n").encode())


def test_recovery_record_rejects_type_corruption_even_with_valid_checksum() -> None:
    payload = json.loads(serialize_recovery_record(_record()))
    payload["snapshot"]["database"]["connection_limit"] = "-1"
    payload["checksum_sha256"] = _canonical_checksum(payload)
    with pytest.raises(ProductionResetError, match="must be an integer"):
        parse_recovery_record((json.dumps(payload) + "\n").encode())


def test_recovery_record_rejects_identity_mismatch() -> None:
    payload = json.loads(serialize_recovery_record(_record()))
    payload["owner"] = "someone_else"
    payload["checksum_sha256"] = _canonical_checksum(payload)
    with pytest.raises(ProductionResetError, match="identity does not match"):
        parse_recovery_record((json.dumps(payload) + "\n").encode())


def test_recovery_store_is_owner_only_and_never_overwrites(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_record())
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ProductionResetError, match="overwrite or reuse"):
        store.create(_record())


def test_recovery_store_rejects_unsafe_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    store = ResetRecoveryStore(parent / "record.json")
    with pytest.raises(ProductionResetError, match="owner-only"):
        store.create(_record())


def test_recovery_store_enforces_legal_state_transitions(tmp_path: Path) -> None:
    store = _store(tmp_path, _record())
    current = store.load()
    current = store.update_state(current, RECOVERY_STATE_GUARD_INSTALLED)
    assert store.load().state == RECOVERY_STATE_GUARD_INSTALLED
    with pytest.raises(ProductionResetError, match="illegal"):
        store.update_state(current, RECOVERY_STATE_COMPLETED)


def _script_namespace(monkeypatch: pytest.MonkeyPatch):
    namespace = runpy.run_path(str(RESET))
    globals_ = namespace["_ordinary_reset"].__globals__
    globals_["log"] = lambda _message: None
    return globals_


def test_ordinary_reset_writes_recovery_before_guard_or_destructive_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path)
    calls: list[str] = []
    monkeypatch.setitem(ns, "inspect_reset_guard", lambda *_: ResetGuardState(True, "dish", True, None))
    monkeypatch.setitem(ns, "_run_prepare", lambda *, preflight_only: calls.append("preflight" if preflight_only else "prepare"))
    monkeypatch.setitem(ns, "snapshot_database_state", lambda *_: calls.append("snapshot") or _snapshot())
    monkeypatch.setitem(ns, "cluster_system_identifier", lambda *_: "7654321")

    def install(*_a, **_kw):
        assert store.exists()
        assert store.load().state == RECOVERY_STATE_SNAPSHOT_CAPTURED
        calls.append("guard")

    def recreate(*_a, **_kw):
        assert store.load().state == RECOVERY_STATE_GUARD_INSTALLED
        calls.append("recreate")

    monkeypatch.setitem(ns, "install_reset_guard", install)
    monkeypatch.setitem(ns, "recreate_database", recreate)
    monkeypatch.setitem(ns, "restore_database_access", lambda *_: calls.append("restore"))
    monkeypatch.setitem(ns, "clear_reset_guard", lambda *_a, **_kw: calls.append("clear"))
    ns["_ordinary_reset"](
        database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
        expected_database_name="dish_stage_a_prod",
        store=store,
    )
    assert calls == ["preflight", "snapshot", "guard", "recreate", "prepare", "restore", "clear"]
    assert store.load().state == RECOVERY_STATE_COMPLETED


def test_ordinary_retry_refuses_existing_record_before_live_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path, _record(RECOVERY_STATE_PREPARE_FAILED))
    monkeypatch.setitem(ns, "snapshot_database_state", lambda *_: pytest.fail("must not resnapshot"))
    with pytest.raises(ProductionResetError, match="ordinary retry is forbidden"):
        ns["_ordinary_reset"](
            database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
            expected_database_name="dish_stage_a_prod",
            store=store,
        )


def test_ordinary_retry_refuses_database_guard_without_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path)
    monkeypatch.setitem(ns, "inspect_reset_guard", lambda *_: ResetGuardState(True, "dish", False, _record().reset_id))
    monkeypatch.setitem(ns, "snapshot_database_state", lambda *_: pytest.fail("must not resnapshot"))
    with pytest.raises(ProductionResetError, match="artifact is missing"):
        ns["_ordinary_reset"](
            database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
            expected_database_name="dish_stage_a_prod",
            store=store,
        )


def test_prepare_failure_retains_original_snapshot_and_guard_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path)
    monkeypatch.setitem(ns, "inspect_reset_guard", lambda *_: ResetGuardState(True, "dish", True, None))
    monkeypatch.setitem(ns, "snapshot_database_state", lambda *_: _snapshot())
    monkeypatch.setitem(ns, "cluster_system_identifier", lambda *_: "7654321")
    monkeypatch.setitem(ns, "install_reset_guard", lambda *_a, **_kw: None)
    monkeypatch.setitem(ns, "recreate_database", lambda *_a, **_kw: None)

    def prepare(*, preflight_only: bool):
        if not preflight_only:
            raise ProductionResetError("prepare failed")

    monkeypatch.setitem(ns, "_run_prepare", prepare)
    with pytest.raises(ProductionResetError, match="retained original snapshot"):
        ns["_ordinary_reset"](
            database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
            expected_database_name="dish_stage_a_prod",
            store=store,
        )
    assert store.load().state == RECOVERY_STATE_PREPARE_FAILED
    assert store.load().snapshot == _snapshot()


def _patch_resume_identity(ns: dict[str, object], monkeypatch: pytest.MonkeyPatch, state: ResetGuardState) -> None:
    monkeypatch.setitem(ns, "cluster_system_identifier", lambda *_: "7654321")
    monkeypatch.setitem(ns, "inspect_reset_guard", lambda *_: state)


def test_resume_uses_only_retained_original_snapshot_and_reruns_prepare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    record = _record(RECOVERY_STATE_PREPARE_FAILED)
    store = _store(tmp_path, record)
    _patch_resume_identity(ns, monkeypatch, ResetGuardState(True, "dish", True, record.reset_id))
    monkeypatch.setitem(ns, "snapshot_database_state", lambda *_: pytest.fail("resume must never resnapshot"))
    seen: list[object] = []
    monkeypatch.setitem(ns, "_run_prepare", lambda *, preflight_only: seen.append("preflight" if preflight_only else "prepare"))
    monkeypatch.setitem(ns, "recreate_database", lambda _url, snapshot, **_kw: seen.append(snapshot))
    monkeypatch.setitem(ns, "restore_database_access", lambda _url, snapshot: seen.append(("restore", snapshot)))
    monkeypatch.setitem(ns, "clear_reset_guard", lambda *_a, **_kw: seen.append("clear"))
    ns["_resume_reset"](
        database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
        expected_database_name="dish_stage_a_prod",
        store=store,
    )
    assert record.snapshot in seen
    assert ("restore", record.snapshot) in seen
    assert "prepare" in seen
    assert store.load().state == RECOVERY_STATE_COMPLETED


@pytest.mark.parametrize(
    "guard,match",
    [
        (ResetGuardState(False, None, None, None), "target is missing"),
        (ResetGuardState(True, "dish", False, None), "fenced without a guard"),
    ],
)
def test_snapshot_captured_resume_refuses_to_infer_destructive_lineage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guard: ResetGuardState, match: str
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path, _record(RECOVERY_STATE_SNAPSHOT_CAPTURED))
    _patch_resume_identity(ns, monkeypatch, guard)
    monkeypatch.setitem(ns, "recreate_database", lambda *_a, **_kw: pytest.fail("must not mutate"))
    with pytest.raises(ProductionResetError, match=match):
        ns["_resume_reset"](
            database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
            expected_database_name="dish_stage_a_prod",
            store=store,
        )


def test_destructive_resume_allows_only_fenced_unguarded_create_crash_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    record = _record(RECOVERY_STATE_GUARD_INSTALLED)
    store = _store(tmp_path, record)
    _patch_resume_identity(ns, monkeypatch, ResetGuardState(True, "dish", False, None))
    monkeypatch.setitem(ns, "_run_prepare", lambda **_kw: None)
    seen: dict[str, object] = {}
    monkeypatch.setitem(ns, "recreate_database", lambda *_a, **kw: seen.update(kw))
    monkeypatch.setitem(ns, "restore_database_access", lambda *_: None)
    monkeypatch.setitem(ns, "clear_reset_guard", lambda *_a, **_kw: None)
    ns["_resume_reset"](
        database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
        expected_database_name="dish_stage_a_prod",
        store=store,
    )
    assert seen["allow_unguarded_fenced"] is True


def test_guard_reset_id_mismatch_refuses_before_resume_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    store = _store(tmp_path, _record(RECOVERY_STATE_PREPARE_FAILED))
    _patch_resume_identity(ns, monkeypatch, ResetGuardState(True, "dish", True, str(uuid.uuid4())))
    monkeypatch.setitem(ns, "recreate_database", lambda *_a, **_kw: pytest.fail("must not mutate"))
    with pytest.raises(ProductionResetError, match="guard mismatch"):
        ns["_resume_reset"](
            database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
            expected_database_name="dish_stage_a_prod",
            store=store,
        )


@pytest.mark.parametrize("guard_id", [_record().reset_id, None])
def test_access_restored_resume_covers_both_finalization_crash_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guard_id: str | None
) -> None:
    ns = _script_namespace(monkeypatch)
    record = _record(RECOVERY_STATE_ACCESS_RESTORED)
    store = _store(tmp_path, record)
    _patch_resume_identity(ns, monkeypatch, ResetGuardState(True, "dish", True, guard_id))
    calls: list[str] = []
    monkeypatch.setitem(ns, "verify_database_access", lambda *_: calls.append("verify"))
    monkeypatch.setitem(ns, "clear_reset_guard", lambda *_a, **_kw: calls.append("clear"))
    ns["_resume_reset"](
        database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
        expected_database_name="dish_stage_a_prod",
        store=store,
    )
    assert calls[0] == "verify"
    assert ("clear" in calls) is (guard_id is not None)
    assert store.load().state == RECOVERY_STATE_COMPLETED


def test_completed_record_is_verified_but_never_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ns = _script_namespace(monkeypatch)
    record = _record(RECOVERY_STATE_COMPLETED)
    store = _store(tmp_path, record)
    _patch_resume_identity(ns, monkeypatch, ResetGuardState(True, "dish", True, None))
    calls: list[str] = []
    monkeypatch.setitem(ns, "verify_database_access", lambda *_: calls.append("verify"))
    monkeypatch.setitem(ns, "recreate_database", lambda *_a, **_kw: pytest.fail("completed must not mutate"))
    ns["_resume_reset"](
        database_url="postgresql+psycopg://dish:x@localhost/dish_stage_a_prod",
        expected_database_name="dish_stage_a_prod",
        store=store,
    )
    assert calls == ["verify"]


def test_reserved_guard_name_is_not_a_normal_snapshot_setting() -> None:
    assert RESET_GUARD_SETTING == "dish.production_reset_id"
    assert all(setting.name != RESET_GUARD_SETTING for setting in _snapshot().settings)
