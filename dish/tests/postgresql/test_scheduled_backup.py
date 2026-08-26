from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dish_pg import scheduled_backup as backup

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dish-pg-scheduled-backup"
SYSTEMD = ROOT / "deploy" / "systemd"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "DISH_PG_DATABASE_URL": "postgresql+psycopg://backup:secret@db.example/dish_prod",
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_prod",
        "DISH_PG_EXPECTED_SCHEMA_HEAD": "0038_head",
        "DISH_PG_BACKUP_LOCAL_DIR": str(tmp_path / "local"),
        "DISH_PG_BACKUP_OFF_DEVICE_DIR": str(tmp_path / "off-device"),
    }


def _config(tmp_path: Path) -> backup.BackupConfig:
    env = _env(tmp_path)
    (tmp_path / "off-device").mkdir()
    return backup.config_from_environ(env, repo_root=tmp_path)


def _stat(device: int, *, size: int = 1) -> SimpleNamespace:
    return SimpleNamespace(st_dev=device, st_size=size, st_mode=0o100600)


def _write_success_report(
    *,
    local_root: Path,
    off_root: Path,
    backup_id: str,
    completed_at: datetime,
    payload: bytes = b"archive",
    expected_schema_head: str = "0038_head",
    observed_schema_head: str = "0038_head",
) -> tuple[Path, dict[str, object]]:
    report_dir = local_root / backup_id
    report_dir.mkdir(parents=True)
    local_dump = report_dir / "postgresql-authority.dump"
    local_dump.write_bytes(payload)
    digest = backup._sha256(local_dump)
    local_checksum = report_dir / "postgresql-authority.dump.sha256"
    local_checksum.write_text(
        backup._checksum_sidecar(digest, local_dump.name), encoding="utf-8"
    )
    off_dump = off_root / f"{backup_id}.dump"
    off_dump.write_bytes(payload)
    off_checksum = off_root / f"{backup_id}.dump.sha256"
    off_checksum.write_text(
        backup._checksum_sidecar(digest, off_dump.name), encoding="utf-8"
    )
    report = backup._with_report_sha256(
        {
            "format": backup.FORMAT,
            "status": "pass" if expected_schema_head == observed_schema_head else "degraded",
            "ok": True,
            "artifact_ok": True,
            "schema_policy": backup._schema_policy(
                expected_schema_head, observed_schema_head
            ),
            "backup_id": backup_id,
            "started_at": backup._timestamp(completed_at),
            "completed_at": backup._timestamp(completed_at),
            "source_commit": "a" * 40,
            "database": {
                "name": "dish_prod",
                "server_version_num": "170010",
                "schema_head": observed_schema_head,
                "public_table_count": 100,
                "database_url_env": "DISH_PG_DATABASE_URL",
            },
            "tools": {},
            "backup": {
                "path": str(local_dump),
                "checksum_path": str(local_checksum),
                "sha256": digest,
                "size_bytes": len(payload),
                "archive_format": "pg_dump-custom",
            },
            "off_device": {
                "path": str(off_dump),
                "checksum_path": str(off_checksum),
                "sha256": digest,
                "size_bytes": len(payload),
                "independent_device": True,
            },
            "restore_compatibility": {},
            "retention_seconds": backup.DEFAULT_RETENTION_SECONDS,
            "health_max_age_seconds": backup.DEFAULT_MAX_AGE_SECONDS,
        }
    )
    backup._atomic_json(report_dir / "backup-report.json", report)
    return report_dir, report


def test_config_defaults_are_two_hour_health_grace_and_seven_day_retention(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    config = backup.config_from_environ(env, repo_root=tmp_path)

    assert config.retention_seconds == 7 * 24 * 60 * 60
    assert config.max_age_seconds == 2 * 60 * 60
    assert config.local_dir == tmp_path / "local"
    assert config.off_device_dir == tmp_path / "off-device"


def test_config_rejects_missing_off_device_destination(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.pop("DISH_PG_BACKUP_OFF_DEVICE_DIR")
    with pytest.raises(backup.BackupError, match="DISH_PG_BACKUP_OFF_DEVICE_DIR is required"):
        backup.config_from_environ(env, repo_root=tmp_path)


def test_prepare_roots_refuses_missing_mount_path(tmp_path: Path) -> None:
    config = backup.BackupConfig(
        database_url="postgresql+psycopg://u:p@db/dish",
        expected_database_name="dish",
        expected_schema_head="head",
        local_dir=tmp_path / "local",
        off_device_dir=tmp_path / "missing-off-device",
        retention_seconds=10,
        max_age_seconds=10,
        repo_root=tmp_path,
    )
    with pytest.raises(backup.BackupError, match="must already exist"):
        backup._prepare_roots(config)


def test_prepare_off_device_root_refuses_same_device_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(backup, "_directory", lambda path, *, label: _stat(1))

    with pytest.raises(backup.BackupError, match="same filesystem device"):
        backup._prepare_off_device_root(config, local_metadata=_stat(1))


def test_prepare_off_device_root_accepts_same_device_with_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    env["DISH_PG_BACKUP_ALLOW_SAME_DEVICE"] = "1"
    (tmp_path / "off-device").mkdir()
    config = backup.config_from_environ(env, repo_root=tmp_path)
    assert config.allow_same_device is True
    monkeypatch.setattr(backup, "_directory", lambda path, *, label: _stat(1))

    off_device_root = backup._prepare_off_device_root(config, local_metadata=_stat(1))

    assert off_device_root == (tmp_path / "off-device").resolve()


def test_copy_off_device_refuses_same_device_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.dump"
    source.write_bytes(b"archive")
    off_root = tmp_path / "off"
    off_root.mkdir()
    monkeypatch.setattr(backup, "_regular_file", lambda path, *, label: _stat(1))
    monkeypatch.setattr(backup, "_directory", lambda path, *, label: _stat(1))

    with pytest.raises(backup.BackupError, match="same filesystem device"):
        backup._copy_off_device(
            source,
            off_device_root=off_root,
            backup_id="20260813T060000Z-deadbeef",
            expected_sha256=backup._sha256(source),
        )


def test_copy_off_device_accepts_same_device_with_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.dump"
    source.write_bytes(b"archive")
    off_root = tmp_path / "off"
    off_root.mkdir()
    expected = backup._sha256(source)
    monkeypatch.setattr(backup, "_regular_file", lambda path, *, label: _stat(1))
    monkeypatch.setattr(backup, "_directory", lambda path, *, label: _stat(1))

    target, checksum = backup._copy_off_device(
        source,
        off_device_root=off_root,
        backup_id="20260813T060000Z-deadbeef",
        expected_sha256=expected,
        allow_same_device=True,
    )

    assert backup._sha256(target) == expected
    assert checksum.exists()


def test_run_records_missing_off_device_mount_as_failed_attempt(tmp_path: Path) -> None:
    config = backup.BackupConfig(
        database_url="postgresql+psycopg://u:p@db/dish",
        expected_database_name="dish",
        expected_schema_head="head",
        local_dir=tmp_path / "local",
        off_device_dir=tmp_path / "missing-off-device",
        retention_seconds=10,
        max_age_seconds=10,
        repo_root=tmp_path,
    )
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)

    with pytest.raises(backup.BackupError, match="must already exist"):
        backup.run_backup(config, environ={}, now=now, token="deadbeef")

    attempt = json.loads(
        (tmp_path / "local" / "last-attempt.json").read_text(encoding="utf-8")
    )
    assert attempt["ok"] is False
    assert attempt["backup_id"] == "20260813T000000Z-deadbeef"
    assert "must already exist" in attempt["error"]


def test_health_reports_missing_off_device_mount_as_unhealthy(tmp_path: Path) -> None:
    config = backup.BackupConfig(
        database_url="postgresql+psycopg://u:p@db/dish",
        expected_database_name="dish",
        expected_schema_head="head",
        local_dir=tmp_path / "local",
        off_device_dir=tmp_path / "missing-off-device",
        retention_seconds=10,
        max_age_seconds=10,
        repo_root=tmp_path,
    )

    result = backup.health(config, now=datetime(2026, 8, 13, tzinfo=timezone.utc))

    assert result["ok"] is False
    assert result["off_device_destination"] == str(tmp_path / "missing-off-device")
    assert "must already exist" in result["error"]


def test_off_device_copy_is_atomic_and_checksum_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.dump"
    source.write_bytes(b"archive")
    off_root = tmp_path / "off"
    off_root.mkdir()
    expected = backup._sha256(source)
    real_regular = backup._regular_file
    real_directory = backup._directory

    def regular(path: Path, *, label: str) -> SimpleNamespace:
        metadata = real_regular(path, label=label)
        return SimpleNamespace(
            st_dev=1 if path == source else 2,
            st_size=metadata.st_size,
            st_mode=metadata.st_mode,
        )

    def directory(path: Path, *, label: str) -> SimpleNamespace:
        metadata = real_directory(path, label=label)
        return SimpleNamespace(st_dev=2, st_size=metadata.st_size, st_mode=metadata.st_mode)

    monkeypatch.setattr(backup, "_regular_file", regular)
    monkeypatch.setattr(backup, "_directory", directory)

    target, checksum = backup._copy_off_device(
        source,
        off_device_root=off_root,
        backup_id="20260813T060000Z-deadbeef",
        expected_sha256=expected,
    )

    assert backup._sha256(target) == expected
    assert checksum.read_text(encoding="utf-8") == backup._checksum_sidecar(
        expected, target.name
    )
    assert not list(off_root.glob(".*.tmp-*"))


def test_run_uses_restore_compatible_custom_archive_and_prunes_only_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    calls: list[str] = []

    monkeypatch.setattr(
        backup, "_prepare_local_root", lambda config: (local_root, _stat(1))
    )
    monkeypatch.setattr(
        backup,
        "_prepare_off_device_root",
        lambda config, *, local_metadata: off_root,
    )
    monkeypatch.setattr(
        backup,
        "_query_source_identity",
        lambda *args, **kwargs: ("dish_prod", "170010", 100, "0038_head"),
    )
    monkeypatch.setattr(backup, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(backup, "_tool_version", lambda binary, env: f"{binary} 17")

    def fake_run(command: list[object], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if str(command[0]) == "pg_dump":
            calls.append("dump")
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"PGDMP-custom-archive")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if str(command[0]) == "pg_restore" and "--list" in command:
            calls.append("verify")
            return subprocess.CompletedProcess(command, 0, stdout="TOC\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(backup, "_run", fake_run)

    def fake_copy(
        source: Path,
        *,
        off_device_root: Path,
        backup_id: str,
        expected_sha256: str,
        allow_same_device: bool = False,
    ) -> tuple[Path, Path]:
        calls.append("copy")
        target = off_device_root / f"{backup_id}.dump"
        target.write_bytes(source.read_bytes())
        checksum = off_device_root / f"{backup_id}.dump.sha256"
        checksum.write_text(
            backup._checksum_sidecar(expected_sha256, target.name), encoding="utf-8"
        )
        return target, checksum

    monkeypatch.setattr(backup, "_copy_off_device", fake_copy)

    def fake_prune(**kwargs: object) -> list[str]:
        calls.append("prune")
        assert "copy" in calls
        return []

    monkeypatch.setattr(backup, "_prune_retention", fake_prune)
    now = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    report = backup.run_backup(
        config,
        environ={},
        now=now,
        token="deadbeef",
    )

    assert calls.index("copy") < calls.index("prune")
    assert report["backup"]["archive_format"] == "pg_dump-custom"
    assert report["restore_compatibility"]["clean_restore_flags"] == [
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
    ]
    assert report["backup"]["sha256"] == report["off_device"]["sha256"]
    assert report["artifact_ok"] is True
    assert report["schema_policy"] == {
        "status": "pass",
        "ok": True,
        "expected_head": "0038_head",
        "observed_head": "0038_head",
        "schema_match": True,
    }
    assert Path(report["backup"]["path"]).exists()
    assert Path(report["off_device"]["path"]).exists()


def test_schema_head_mismatch_creates_verified_artifact_and_degraded_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    env["DISH_PG_EXPECTED_SCHEMA_HEAD"] = "0040_no_asana_post_burn"
    off_root = tmp_path / "off-device"
    off_root.mkdir()
    config = backup.config_from_environ(env, repo_root=tmp_path)
    local_root = tmp_path / "local"
    calls: list[str] = []

    monkeypatch.setattr(
        backup, "_prepare_local_root", lambda config: (local_root, _stat(1))
    )
    monkeypatch.setattr(
        backup,
        "_prepare_off_device_root",
        lambda config, *, local_metadata: off_root,
    )
    monkeypatch.setattr(
        backup,
        "_query_source_identity",
        lambda *args, **kwargs: (
            "dish_prod",
            "170010",
            100,
            "0041_test_generation_rollover",
        ),
    )
    monkeypatch.setattr(backup, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(backup, "_tool_version", lambda binary, env: f"{binary} 17")

    def fake_run(
        command: list[object], *, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if str(command[0]) == "pg_dump":
            calls.append("dump")
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMP-archive")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if str(command[0]) == "pg_restore" and "--list" in command:
            calls.append("verify")
            return subprocess.CompletedProcess(command, 0, stdout="TOC\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(backup, "_run", fake_run)

    def fake_copy(
        source: Path,
        *,
        off_device_root: Path,
        backup_id: str,
        expected_sha256: str,
        allow_same_device: bool = False,
    ) -> tuple[Path, Path]:
        calls.append("copy")
        target = off_device_root / f"{backup_id}.dump"
        target.write_bytes(source.read_bytes())
        checksum = off_device_root / f"{backup_id}.dump.sha256"
        checksum.write_text(
            backup._checksum_sidecar(expected_sha256, target.name), encoding="utf-8"
        )
        return target, checksum

    monkeypatch.setattr(backup, "_copy_off_device", fake_copy)
    monkeypatch.setattr(backup, "_prune_retention", lambda **kwargs: [])

    completed = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
    report = backup.run_backup(
        config, environ={}, now=completed, token="deadbeef"
    )

    assert calls == ["dump", "verify", "copy", "verify"]
    assert report["ok"] is True
    assert report["artifact_ok"] is True
    assert report["status"] == "degraded"
    assert report["schema_policy"] == {
        "status": "degraded",
        "ok": False,
        "expected_head": "0040_no_asana_post_burn",
        "observed_head": "0041_test_generation_rollover",
        "schema_match": False,
    }
    assert Path(report["backup"]["path"]).exists()
    assert Path(report["off_device"]["path"]).exists()
    attempt = json.loads((local_root / "last-attempt.json").read_text(encoding="utf-8"))
    assert attempt["ok"] is True

    monkeypatch.setattr(backup, "_prepare_roots", lambda config: (local_root, off_root))
    real_regular = backup._regular_file

    def regular(path: Path, *, label: str) -> SimpleNamespace:
        metadata = real_regular(path, label=label)
        device = 2 if path.parent == off_root else 1
        return SimpleNamespace(st_dev=device, st_size=metadata.st_size, st_mode=metadata.st_mode)

    monkeypatch.setattr(backup, "_regular_file", regular)
    health = backup.health(config, now=completed + timedelta(hours=1))

    assert health["ok"] is False
    assert health["status"] == "degraded"
    assert health["artifact_ok"] is True
    assert health["artifact_fresh"] is True
    assert health["schema_policy_ok"] is False
    assert health["schema_policy"] == report["schema_policy"]
    assert "verified artifact remains usable" in health["error"]


def test_run_command_returns_failure_signal_for_degraded_schema_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    report = {
        "ok": True,
        "artifact_ok": True,
        "backup_id": "20260826T080000Z-deadbeef",
        "completed_at": "2026-08-26T08:00:00Z",
        "backup": {"sha256": "a" * 64},
        "off_device": {"path": "/off/backup.dump"},
        "database": {"schema_head": "0041_test_generation_rollover"},
        "schema_policy": backup._schema_policy(
            "0040_no_asana_post_burn", "0041_test_generation_rollover"
        ),
    }
    monkeypatch.setattr(backup, "config_from_environ", lambda **kwargs: config)
    monkeypatch.setattr(backup, "run_backup", lambda config: report)

    assert backup.main(["run"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["artifact_ok"] is True
    assert output["status"] == "degraded"
    assert output["schema_policy"]["schema_match"] is False


@pytest.mark.parametrize(
    ("source_identity", "message"),
    [
        (("wrong_database", "170010", 100, "0038_head"), "connected database"),
        (("dish_prod", "170010", 0, "0038_head"), "public schema has no tables"),
    ],
)
def test_pre_dump_database_and_schema_gates_remain_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_identity: tuple[str, str, int, str],
    message: str,
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        backup, "_prepare_local_root", lambda config: (local_root, _stat(1))
    )
    monkeypatch.setattr(
        backup,
        "_prepare_off_device_root",
        lambda config, *, local_metadata: off_root,
    )
    monkeypatch.setattr(
        backup, "_query_source_identity", lambda *args, **kwargs: source_identity
    )
    monkeypatch.setattr(
        backup,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pg_dump ran")),
    )

    with pytest.raises(backup.BackupError, match=message):
        backup.run_backup(
            config,
            environ={},
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
            token="deadbeef",
        )


def test_multiple_alembic_heads_remain_fatal_before_pg_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        backup, "_prepare_local_root", lambda config: (local_root, _stat(1))
    )
    monkeypatch.setattr(
        backup,
        "_prepare_off_device_root",
        lambda config, *, local_metadata: off_root,
    )
    monkeypatch.setattr(
        backup,
        "_query_source_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            backup.BackupError("database must have exactly one Alembic head, found 2")
        ),
    )
    monkeypatch.setattr(
        backup,
        "_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pg_dump ran")),
    )

    with pytest.raises(backup.BackupError, match="exactly one Alembic head"):
        backup.run_backup(
            config,
            environ={},
            now=datetime(2026, 8, 26, tzinfo=timezone.utc),
            token="deadbeef",
        )


def test_copy_failure_never_invokes_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    previous_id = "20260801T000000Z-11111111"
    _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id=previous_id,
        completed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        payload=b"previous-usable",
    )

    monkeypatch.setattr(
        backup, "_prepare_local_root", lambda config: (local_root, _stat(1))
    )
    monkeypatch.setattr(
        backup,
        "_prepare_off_device_root",
        lambda config, *, local_metadata: off_root,
    )
    monkeypatch.setattr(
        backup,
        "_query_source_identity",
        lambda *args, **kwargs: ("dish_prod", "170010", 100, "0038_head"),
    )
    monkeypatch.setattr(backup, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(backup, "_tool_version", lambda *args, **kwargs: "17")

    def fake_run(command: list[object], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        if str(command[0]) == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"archive")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="TOC\n", stderr="")

    monkeypatch.setattr(backup, "_run", fake_run)
    monkeypatch.setattr(
        backup,
        "_copy_off_device",
        lambda *args, **kwargs: (_ for _ in ()).throw(backup.BackupError("copy failed")),
    )
    monkeypatch.setattr(
        backup,
        "_prune_retention",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("retention ran before copy success")),
    )

    with pytest.raises(backup.BackupError, match="copy failed"):
        backup.run_backup(
            config,
            environ={},
            now=datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc),
            token="deadbeef",
        )

    assert (local_root / previous_id / "postgresql-authority.dump").read_bytes() == b"previous-usable"
    assert (off_root / f"{previous_id}.dump").read_bytes() == b"previous-usable"
    attempt = json.loads((local_root / "last-attempt.json").read_text(encoding="utf-8"))
    assert attempt["ok"] is False
    assert "copy failed" in attempt["error"]


def test_retention_removes_only_expired_successful_backup_after_current_exists(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"
    off_root = tmp_path / "off"
    local_root.mkdir()
    off_root.mkdir()
    old_time = datetime(2026, 8, 1, tzinfo=timezone.utc)
    recent_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260801T000000Z-11111111",
        completed_at=old_time,
    )
    _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260812T000000Z-22222222",
        completed_at=recent_time,
    )

    deleted = backup._prune_retention(
        local_root=local_root,
        off_device_root=off_root,
        retention_tiers=(backup.RetentionTier(7 * 24 * 60 * 60, 0),),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
        current_backup_id="20260813T000000Z-33333333",
    )

    assert deleted == ["20260801T000000Z-11111111"]
    assert not (local_root / "20260801T000000Z-11111111").exists()
    assert (local_root / "20260812T000000Z-22222222").exists()
    assert not (off_root / "20260801T000000Z-11111111.dump").exists()
    assert (off_root / "20260812T000000Z-22222222.dump").exists()


def test_config_default_retention_tiers_is_empty_and_backward_compatible(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    config = backup.config_from_environ(env, repo_root=tmp_path)

    assert config.retention_tiers == ()
    assert config.retention_seconds == 7 * 24 * 60 * 60
    assert backup._effective_retention_tiers(config) == (
        backup.RetentionTier(7 * 24 * 60 * 60, 0),
    )


def test_config_parses_retention_tiers_and_derives_retention_seconds(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    env["DISH_PG_BACKUP_RETENTION_TIERS"] = "86400:0,604800:14400,7776000:86400"
    config = backup.config_from_environ(env, repo_root=tmp_path)

    assert config.retention_tiers == (
        backup.RetentionTier(86400, 0),
        backup.RetentionTier(604800, 14400),
        backup.RetentionTier(7776000, 86400),
    )
    assert config.retention_seconds == 7776000


def test_config_rejects_non_increasing_retention_tiers(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["DISH_PG_BACKUP_RETENTION_TIERS"] = "86400:0,3600:0"
    with pytest.raises(backup.BackupError, match="strictly increasing"):
        backup.config_from_environ(env, repo_root=tmp_path)


def test_prune_retention_keeps_full_density_within_first_tier(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    off_root = tmp_path / "off"
    local_root.mkdir()
    off_root.mkdir()
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    ids = []
    for hour_ago in range(1, 24):
        completed_at = now - timedelta(hours=hour_ago)
        backup_id = f"{completed_at.strftime('%Y%m%dT%H%M%SZ')}-{hour_ago:08x}"
        _write_success_report(
            local_root=local_root,
            off_root=off_root,
            backup_id=backup_id,
            completed_at=completed_at,
        )
        ids.append(backup_id)

    deleted = backup._prune_retention(
        local_root=local_root,
        off_device_root=off_root,
        retention_tiers=(
            backup.RetentionTier(24 * 60 * 60, 0),
            backup.RetentionTier(7 * 24 * 60 * 60, 4 * 60 * 60),
        ),
        now=now,
        current_backup_id="current",
    )

    assert deleted == []
    for backup_id in ids:
        assert (local_root / backup_id).exists()


def test_prune_retention_thins_second_tier_to_one_per_bucket(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    off_root = tmp_path / "off"
    local_root.mkdir()
    off_root.mkdir()
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)

    # Two hourly backups landing in the same 4h bucket, 30h and 29h old
    # (both within the 24h-7d band): only the newer one should survive.
    older = now - timedelta(hours=30)
    newer = now - timedelta(hours=29)
    older_id = f"{older.strftime('%Y%m%dT%H%M%SZ')}-11111111"
    newer_id = f"{newer.strftime('%Y%m%dT%H%M%SZ')}-22222222"
    _write_success_report(
        local_root=local_root, off_root=off_root, backup_id=older_id, completed_at=older
    )
    _write_success_report(
        local_root=local_root, off_root=off_root, backup_id=newer_id, completed_at=newer
    )

    deleted = backup._prune_retention(
        local_root=local_root,
        off_device_root=off_root,
        retention_tiers=(
            backup.RetentionTier(24 * 60 * 60, 0),
            backup.RetentionTier(7 * 24 * 60 * 60, 4 * 60 * 60),
        ),
        now=now,
        current_backup_id="current",
    )

    assert deleted == [older_id]
    assert not (local_root / older_id).exists()
    assert (local_root / newer_id).exists()


def test_prune_retention_deletes_backups_beyond_outer_tier(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    off_root = tmp_path / "off"
    local_root.mkdir()
    off_root.mkdir()
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    ancient = now - timedelta(days=200)
    ancient_id = f"{ancient.strftime('%Y%m%dT%H%M%SZ')}-99999999"
    _write_success_report(
        local_root=local_root, off_root=off_root, backup_id=ancient_id, completed_at=ancient
    )

    deleted = backup._prune_retention(
        local_root=local_root,
        off_device_root=off_root,
        retention_tiers=(
            backup.RetentionTier(86400, 0),
            backup.RetentionTier(604800, 14400),
            backup.RetentionTier(7776000, 86400),
        ),
        now=now,
        current_backup_id="current",
    )

    assert deleted == [ancient_id]
    assert not (local_root / ancient_id).exists()


def test_health_reports_latest_age_destination_and_freshness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    completed = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    report_dir, _report = _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260813T060000Z-deadbeef",
        completed_at=completed,
    )
    backup._write_attempt(
        local_root,
        backup_id=report_dir.name,
        started_at=completed,
        completed_at=completed,
        ok=True,
    )
    monkeypatch.setattr(backup, "_prepare_roots", lambda config: (local_root, off_root))
    real_regular = backup._regular_file

    def regular(path: Path, *, label: str) -> SimpleNamespace:
        metadata = real_regular(path, label=label)
        device = 2 if path.parent == off_root else 1
        return SimpleNamespace(st_dev=device, st_size=metadata.st_size, st_mode=metadata.st_mode)

    monkeypatch.setattr(backup, "_regular_file", regular)
    result = backup.health(
        config,
        now=completed + timedelta(hours=1, minutes=30),
    )

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["fresh"] is True
    assert result["artifact_ok"] is True
    assert result["artifact_fresh"] is True
    assert result["schema_policy_ok"] is True
    assert result["schema_policy"]["schema_match"] is True
    assert result["latest_success"]["age_seconds"] == 1.5 * 60 * 60
    assert result["latest_success"]["off_device_path"].endswith("deadbeef.dump")
    assert result["off_device_destination"] == str(off_root)


def test_health_fails_for_stale_backup_or_newer_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    completed = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    report_dir, _report = _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260813T000000Z-deadbeef",
        completed_at=completed,
    )
    monkeypatch.setattr(backup, "_prepare_roots", lambda config: (local_root, off_root))
    real_regular = backup._regular_file

    def regular(path: Path, *, label: str) -> SimpleNamespace:
        metadata = real_regular(path, label=label)
        device = 2 if path.parent == off_root else 1
        return SimpleNamespace(st_dev=device, st_size=metadata.st_size, st_mode=metadata.st_mode)

    monkeypatch.setattr(backup, "_regular_file", regular)
    stale = backup.health(config, now=completed + timedelta(hours=8))
    assert stale["ok"] is False
    assert stale["fresh"] is False
    assert "stale" in stale["error"]

    backup._write_attempt(
        local_root,
        backup_id=report_dir.name,
        started_at=completed + timedelta(hours=1),
        completed_at=completed + timedelta(hours=1),
        ok=False,
        error="off-device copy failed",
    )
    failed = backup.health(config, now=completed + timedelta(hours=2))
    assert failed["fresh"] is True
    assert failed["ok"] is False
    assert failed["latest_attempt_ok"] is False
    assert "latest backup attempt failed" in failed["error"]


def test_health_refuses_same_device_latest_backup_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    completed = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260813T060000Z-deadbeef",
        completed_at=completed,
    )
    monkeypatch.setattr(backup, "_prepare_roots", lambda config: (local_root, off_root))
    monkeypatch.setattr(backup, "_regular_file", lambda path, *, label: _stat(1))

    result = backup.health(config, now=completed)

    assert result["ok"] is False
    assert "independent device" in result["error"]


def test_health_accepts_same_device_latest_backup_with_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    env["DISH_PG_BACKUP_ALLOW_SAME_DEVICE"] = "1"
    (tmp_path / "off-device").mkdir()
    config = backup.config_from_environ(env, repo_root=tmp_path)
    local_root = tmp_path / "local"
    off_root = tmp_path / "off-device"
    local_root.mkdir(exist_ok=True)
    completed = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    _write_success_report(
        local_root=local_root,
        off_root=off_root,
        backup_id="20260813T060000Z-deadbeef",
        completed_at=completed,
    )
    monkeypatch.setattr(backup, "_prepare_roots", lambda config: (local_root, off_root))
    monkeypatch.setattr(backup, "_regular_file", lambda path, *, label: _stat(1))

    result = backup.health(
        config, now=completed + timedelta(hours=1, minutes=30)
    )

    assert result["ok"] is True
    assert result["latest_success"]["off_device_independent"] is False


def test_systemd_default_schedule_is_hourly_persistent_and_configurable() -> None:
    service = (SYSTEMD / "dish-postgres-backup.service").read_text(encoding="utf-8")
    timer = (SYSTEMD / "dish-postgres-backup.timer").read_text(encoding="utf-8")
    override = (SYSTEMD / "postgres-backup-cadence.conf.example").read_text(
        encoding="utf-8"
    )
    env = (SYSTEMD / "postgres-backup.env.example").read_text(encoding="utf-8")

    assert "Requires=dish-postgres-prod.service" not in service
    assert "After=network-online.target dish-postgres-prod.service" in service
    assert "[Install]" not in service
    assert "OnCalendar=*-*-* *:00:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "OnCalendar=\nOnCalendar=" in override
    assert "DISH_PG_BACKUP_RETENTION_SECONDS=604800" in env
    assert "DISH_PG_BACKUP_RETENTION_TIERS=86400:0,604800:14400,7776000:86400" in env
    assert "DISH_PG_BACKUP_MAX_AGE_SECONDS=7200" in env


def test_cli_exposes_run_health_and_status_alias() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0
    assert "run" in completed.stdout
    assert "health" in completed.stdout
    assert "status" in completed.stdout


def test_command_errors_redact_database_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["psql"],
            1,
            stdout="postgresql://user:secret@host/db",
            stderr="postgresql+psycopg://user:secret@host/db",
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(backup.BackupError) as caught:
        backup._run(["psql", "postgresql://user:secret@host/db"], env={})
    assert "secret" not in str(caught.value)
    assert "<redacted>@host/db" in str(caught.value)
