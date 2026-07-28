from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from dish_service.backup import BackupManager
from dish_service.database_ownership import (
    ServiceDatabaseOwnership,
    service_process_lock_path,
)
from dish_service.process_lock import ServiceProcessLock
from dish_tool.database_schema import MIGRATIONS, initialize_database
from dish_tool.errors import DishRuleError
from tests.test_dish_tool_r46_operational_hardening import _service


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _v2_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);"
        + MIGRATIONS[1]
        + "\nINSERT INTO schema_migrations VALUES (1, 't');\n"
        + MIGRATIONS[2]
        + "\nINSERT INTO schema_migrations VALUES (2, 't');\nPRAGMA user_version=2;"
    )
    return conn


def test_legacy_backup_includes_committed_wal_pages(tmp_path):
    db_path = tmp_path / "dish.db"
    writer = _v2_database(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        """INSERT INTO submissions(
               submission_id,task_gid,submission_kind,protocol_release,
               release_commit,protocol_bundle,canonical_manifest,editor_agent,
               editor_family,status,created_at
           ) VALUES('wal-row','task','planning','old','abc','{}','{}',
                    'claude','claude','ready','2026-07-28T00:00:00Z')"""
    )
    assert Path(str(db_path) + "-wal").exists()

    migrated = initialize_database(db_path)
    migrated.close()
    writer.close()

    backup = db_path.with_suffix(".db.legacy-v2.bak")
    legacy = sqlite3.connect(backup)
    try:
        assert legacy.execute(
            "SELECT status FROM submissions WHERE submission_id='wal-row'"
        ).fetchone()[0] == "ready"
        assert legacy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        legacy.close()


def test_invalid_existing_legacy_backup_is_replaced(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = _v2_database(db_path)
    conn.close()
    backup = db_path.with_suffix(".db.legacy-v2.bak")
    backup.write_bytes(b"not a sqlite backup")

    upgraded = initialize_database(db_path)
    upgraded.close()

    legacy = sqlite3.connect(backup)
    try:
        assert legacy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 2
        assert legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submissions'"
        ).fetchone()
    finally:
        legacy.close()


def test_database_ownership_and_process_lock_follow_symlink_identity(tmp_path):
    target = tmp_path / "real" / "shared.db"
    target.parent.mkdir()
    target.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)

    real_owner = ServiceDatabaseOwnership(target)
    alias_owner = ServiceDatabaseOwnership(alias)
    assert real_owner.db_path == alias_owner.db_path
    assert real_owner.path == alias_owner.path
    real_owner.mark()
    with pytest.raises(DishRuleError) as exc:
        alias_owner.assert_local_access_allowed()
    assert exc.value.rule == "service_owned_database"

    real_lock = service_process_lock_path(target)
    alias_lock = service_process_lock_path(alias)
    assert real_lock == alias_lock
    held = ServiceProcessLock(real_lock).acquire()
    try:
        with pytest.raises(DishRuleError) as lock_exc:
            ServiceProcessLock(alias_lock).acquire()
        assert lock_exc.value.rule == "service_process_lock_held"
    finally:
        held.release()


def test_managed_backup_filename_symlink_is_rejected(tmp_path):
    live = tmp_path / "live.db"
    initialize_database(live).close()
    outside = tmp_path / "outside.db"
    initialize_database(outside).close()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    link = backup_dir / "dish-malicious.sqlite3"
    link.symlink_to(outside)

    manager = BackupManager(live, backup_dir)
    with pytest.raises(DishRuleError) as exc:
        manager.restore(link.name)
    assert exc.value.rule == "backup_path_symlink"


def test_restore_metadata_hashes_actual_migrated_bytes(tmp_path):
    service, _backend = _service(tmp_path)
    source = service.config.backup_dir / "dish-schema-20.sqlite3"
    source.parent.mkdir(parents=True, exist_ok=True)
    old = initialize_database(source)
    old.execute("DROP TABLE service_requests")
    old.execute("DROP TABLE operation_execution_claims")
    old.execute("DROP TABLE operation_executions")
    old.execute("DROP INDEX write_attempts_one_unresolved_operation")
    old.execute("DROP INDEX movement_attempts_one_unresolved_operation")
    old.execute("DELETE FROM schema_migrations WHERE version>=21")
    old.execute("PRAGMA user_version=20")
    old.close()
    source_hash = _sha(source)

    initialize_database(service.config.db_path).close()
    restored = service.restore_backup(source.name)
    assert restored["ok"]
    live_hash = _sha(service.config.db_path)
    assert restored["data"]["restored"]["backup_id"] == source.name
    assert restored["data"]["restored"]["sha256"] == live_hash
    assert restored["data"]["restored"]["size_bytes"] == service.config.db_path.stat().st_size
    assert live_hash != source_hash


def test_restore_refuses_to_start_without_durable_lockout(monkeypatch, tmp_path):
    service, _backend = _service(tmp_path)
    restore_called = False

    def fail_marker(details):
        raise OSError("marker unavailable")

    def unexpected_restore(backup_id):
        nonlocal restore_called
        restore_called = True
        raise AssertionError("restore must not start")

    monkeypatch.setattr(service._restore_fault, "set", fail_marker)
    monkeypatch.setattr(BackupManager, "restore", lambda self, backup_id: unexpected_restore(backup_id))
    result = service.restore_backup("dish-source.sqlite3")
    assert result["code"] == "INTERNAL_ERROR"
    assert result["errors"][0]["rule"] == "restore_lockout_persistence_failed"
    assert result["errors"][0]["database_retained"] is True
    assert restore_called is False


def test_prearmed_restore_lockout_survives_enrichment_failure(monkeypatch, tmp_path):
    service, _backend = _service(tmp_path)
    original_set = service._restore_fault.set
    calls = 0

    def set_once(details):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_set(details)
        raise OSError("cannot enrich marker")

    monkeypatch.setattr(service._restore_fault, "set", set_once)
    monkeypatch.setattr(
        BackupManager,
        "restore",
        lambda self, backup_id: (_ for _ in ()).throw(RuntimeError("unknown outcome")),
    )
    result = service.restore_backup("dish-source.sqlite3")
    assert result["errors"][0]["rule"] == "backup_restore_recovery_unknown"
    fault = service._restore_fault.read()
    assert fault is not None
    assert fault["kind"] == "backup_restore_in_progress"
    assert service.health()["maintenance"]["restore_recovery_required"] is True
