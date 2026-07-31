from __future__ import annotations

import sqlite3
from pathlib import Path

from dish_service import backup as backup_module
from dish_service.leases import ServicePrincipal
from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database_schema import initialize_database
from tests.support.operational import _service


def _error(result: dict) -> dict:
    assert result["ok"] is False
    assert len(result["errors"]) == 1
    return result["errors"][0]


def test_backup_permission_failure_names_destination_not_live_database(monkeypatch, tmp_path):
    service, _backend = _service(tmp_path)
    initialize_database(service.config.db_path).close()
    original = backup_module.tempfile.NamedTemporaryFile

    def denied(*args, **kwargs):
        if Path(kwargs["dir"]) == service.config.backup_dir:
            raise PermissionError("managed directory is read-only")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(backup_module.tempfile, "NamedTemporaryFile", denied)
        result = service.create_backup(label="blocked")

    error = _error(result)
    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert error == {
        "rule": "backup_destination_unavailable",
        "resource": "managed_backup_directory",
        "reason": "permission_denied",
        "error_type": "PermissionError",
        "database_retained": True,
    }
    assert service.health()["database"]["ok"] is True

    recovered = service.create_backup(label="recovered")
    assert recovered["ok"] is True


def test_backup_missing_directory_is_created_but_file_path_is_rejected(tmp_path):
    missing_root = tmp_path / "missing-parent"
    missing_root.mkdir()
    service, _backend = _service(missing_root)
    assert service.config.backup_dir.exists() is False
    created = service.create_backup(label="created")
    assert created["ok"] is True

    blocked_root = tmp_path / "blocked-parent"
    blocked_root.mkdir()
    blocked, _backend = _service(blocked_root)
    blocked.config.backup_dir.write_text("not a directory", encoding="utf-8")
    result = blocked.create_backup(label="blocked")
    error = _error(result)
    assert result["code"] == "BACKEND_REJECTED"
    assert error["rule"] == "backup_destination_unavailable"
    assert error["resource"] == "managed_backup_directory"
    assert error["reason"] == "not_directory"
    assert error["database_retained"] is True
    assert blocked.health()["database"]["ok"] is True


def test_invalid_immutable_backup_is_nonretryable_and_exactly_replayed(tmp_path):
    service, _backend = _service(tmp_path)
    service.config.backup_dir.mkdir(parents=True)
    invalid = service.config.backup_dir / "dish-invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    principal = ServicePrincipal("admin", "restore-run")
    request_id = "30000000-0000-4000-8000-000000000001"

    first = service.restore_backup(
        invalid.name, principal=principal, request_id=request_id
    )
    error = _error(first)
    assert first["code"] == "VALIDATION_FAILED", first
    assert first["retryable"] is False
    assert error["rule"] == "backup_database_invalid"
    assert error["backup_id"] == invalid.name
    assert error["immutable_input"] is True

    replay = service.restore_backup(
        invalid.name, principal=principal, request_id=request_id
    )
    assert replay["code"] == first["code"]
    assert replay["retryable"] is False
    assert replay["errors"] == first["errors"]
    assert replay["data"]["request_replayed"] is True


def test_schema_invalid_immutable_backup_is_nonretryable_without_replacement(tmp_path):
    service, _backend = _service(tmp_path)
    service.config.backup_dir.mkdir(parents=True)
    invalid = service.config.backup_dir / "dish-schema-invalid.sqlite3"
    source = initialize_database(invalid)
    source.execute("DROP TABLE operations")
    source.close()
    live = initialize_database(service.config.db_path)
    live.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    live.execute("INSERT INTO sentinel VALUES ('retained')")
    live.close()

    result = service.restore_backup(invalid.name)

    error = _error(result)
    assert result["code"] == "VALIDATION_FAILED"
    assert result["retryable"] is False
    assert error["rule"] == "database_schema_incomplete"
    assert error["backup_id"] == invalid.name
    assert error["immutable_input"] is True
    live = sqlite3.connect(service.config.db_path)
    try:
        assert live.execute("SELECT value FROM sentinel").fetchone()[0] == "retained"
    finally:
        live.close()


def test_restore_response_separates_source_from_installed_database(tmp_path):
    service, _backend = _service(tmp_path)
    created = service.create_backup(label="source")
    assert created["ok"] is True
    backup_id = created["data"]["backup"]["backup_id"]

    restored = service.restore_backup(backup_id)

    assert restored["ok"] is True
    metadata = restored["data"]["restored"]
    assert metadata["source_backup_id"] == backup_id
    assert metadata["source_schema_version"] == SCHEMA_VERSION
    assert "backup_id" not in metadata
    assert set(metadata["installed_database"]) == {
        "sha256",
        "size_bytes",
        "schema_version",
    }
    assert metadata["installed_database"]["schema_version"] == SCHEMA_VERSION
    assert "source_schema_version" not in restored["data"]
    assert "restored_schema_version" not in restored["data"]
