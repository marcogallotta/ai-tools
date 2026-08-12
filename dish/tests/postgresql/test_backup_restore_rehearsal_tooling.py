from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from dish_pg import backup_restore_rehearsal as rehearsal

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dish-pg-backup-restore-rehearsal"


def _args(tmp_path: Path, *, source_url_env: str = "DISH_PG_REHEARSAL_URL") -> Namespace:
    operations_tool = tmp_path / "operations"
    operations_tool.write_text("tool", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return Namespace(
        source_commit="a" * 40,
        expected_schema_head="0038_head",
        expected_source_database="source",
        expected_restore_database="restore",
        output_dir=tmp_path / "evidence",
        retention_destination=tmp_path / "retained.dump",
        source_url_env=source_url_env,
        source_libpq_url_env=None,
        restore_url_env="DISH_PG_RESTORE_URL",
        restore_libpq_url_env=None,
        operations_tool=operations_tool,
        repo_root=repo_root,
        python="python",
        pg_dump="pg_dump",
        pg_restore="pg_restore",
        psql="psql",
    )


def _env() -> dict[str, str]:
    return {
        "DISH_PG_REHEARSAL_URL": "postgresql+psycopg://source:secret@db/rehearsal",
        "DISH_PG_REHEARSAL_LIBPQ_URL": "postgresql://source:secret@db/rehearsal",
        "DISH_PG_RESTORE_URL": "postgresql+psycopg://restore:secret@db/restore",
        "DISH_PG_RESTORE_LIBPQ_URL": "postgresql://restore:secret@db/restore",
    }


def test_canonical_database_url_derives_the_only_libpq_target() -> None:
    canonical, libpq = rehearsal._canonical_target(
        "postgresql+psycopg://user:p%40ss@db.example:5544/dish?sslmode=require",
        label="source",
    )

    assert libpq.startswith("postgresql://")
    assert "+psycopg" not in libpq
    assert rehearsal._target_signature(rehearsal.make_url(libpq)) == rehearsal._target_signature(
        canonical.set(drivername="postgresql")
    )


def test_matching_optional_libpq_assertion_is_only_an_assertion() -> None:
    canonical, _libpq = rehearsal._canonical_target(
        "postgresql+psycopg://user:secret@db.example:5544/dish?sslmode=require",
        label="source",
    )

    assert rehearsal._assert_libpq_binding(
        canonical=canonical,
        asserted_value="postgresql://user:secret@db.example:5544/dish?sslmode=require",
        asserted_env="SOURCE_LIBPQ_ASSERT",
        label="source",
    ) is True


def test_mismatched_source_target_forms_fail_before_backup_or_database_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.source_libpq_url_env = "DISH_PG_REHEARSAL_LIBPQ_URL"
    env = _env()
    env["DISH_PG_REHEARSAL_LIBPQ_URL"] = (
        "postgresql://source:secret@different-instance/rehearsal"
    )

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError("database/external command ran before target binding")

    monkeypatch.setattr(rehearsal, "_git_head", unexpected)
    with pytest.raises(
        rehearsal.RehearsalError, match="source libpq assertion.*does not match"
    ):
        rehearsal.run(args, environ=env)

    assert not (args.output_dir / "postgresql-authority.dump").exists()
    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    assert report["ok"] is False
    assert "checkout_commit" not in report


def test_mismatched_restore_target_forms_fail_before_backup_or_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    args.restore_libpq_url_env = "DISH_PG_RESTORE_LIBPQ_URL"
    env = _env()
    env["DISH_PG_RESTORE_LIBPQ_URL"] = (
        "postgresql://restore:secret@different-instance/restore"
    )

    def unexpected(*args: object, **kwargs: object) -> str:
        raise AssertionError("database/external command ran before target binding")

    monkeypatch.setattr(rehearsal, "_git_head", unexpected)
    with pytest.raises(
        rehearsal.RehearsalError, match="restore libpq assertion.*does not match"
    ):
        rehearsal.run(args, environ=env)

    assert not (args.output_dir / "postgresql-authority.dump").exists()
    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    assert report["ok"] is False
    assert "checkout_commit" not in report


def test_off_device_copy_requires_independent_device_and_matching_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "backup.dump"
    source.write_bytes(b"backup")
    destination = tmp_path / "retained" / "backup.dump"
    real_stat = Path.stat

    class Stat:
        def __init__(self, base: object, device: int) -> None:
            for name in dir(base):
                if name.startswith("st_"):
                    setattr(self, name, getattr(base, name))
            self.st_dev = device

    def fake_stat(path: Path, *args: object, **kwargs: object) -> Stat:
        base = real_stat(path, *args, **kwargs)
        return Stat(base, 2 if path == destination.parent else 1)

    monkeypatch.setattr(Path, "stat", fake_stat)
    result = rehearsal._copy_off_device(source, destination)

    assert result["independent_device"] is True
    assert result["sha256"] == rehearsal._sha256(source)
    assert destination.stat().st_mode & 0o077 == 0


def test_off_device_copy_fails_closed_on_same_device(tmp_path: Path) -> None:
    source = tmp_path / "backup.dump"
    source.write_bytes(b"backup")

    with pytest.raises(rehearsal.RehearsalError, match="same filesystem device"):
        rehearsal._copy_off_device(source, tmp_path / "copy.dump")


def test_run_rejects_protected_authority_environment_and_hashes_failure(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, source_url_env="DISH_PG_URL")
    env = _env()
    env["DISH_PG_URL"] = "postgresql+psycopg://prod:secret@db/prod"

    with pytest.raises(
        rehearsal.RehearsalError, match="protected authority/test environment"
    ):
        rehearsal.run(args, environ=env)

    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    report_sha256 = report.pop("report_sha256")
    assert report["ok"] is False
    assert report["status"] == "fail"
    assert report_sha256 == hashlib.sha256(rehearsal._canonical(report)).hexdigest()
    assert "secret" not in json.dumps(report)


def test_run_rejects_alias_of_protected_authority_environment(tmp_path: Path) -> None:
    args = _args(tmp_path)
    env = _env()
    env["DISH_PG_URL"] = env["DISH_PG_REHEARSAL_URL"]

    with pytest.raises(rehearsal.RehearsalError, match="aliases protected"):
        rehearsal.run(args, environ=env)


def test_command_failure_redacts_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["psql"],
            1,
            stdout="postgresql://user:secret@host/db",
            stderr="postgresql+psycopg://user:secret@host/db",
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(rehearsal.RehearsalError) as caught:
        rehearsal._run(
            ["psql", "postgresql://user:secret@host/db"],
            env={},
        )
    assert "secret" not in str(caught.value)
    assert "<redacted>@host/db" in str(caught.value)


def test_cli_exposes_governed_rehearsal_contract() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0
    for flag in (
        "--source-commit",
        "--expected-schema-head",
        "--retention-destination",
        "--expected-restore-database",
        "--source-libpq-url-env",
        "--restore-libpq-url-env",
        "--repo-root",
    ):
        assert flag in completed.stdout


def test_invalid_or_nonpassing_evidence_fails_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    with pytest.raises(rehearsal.RehearsalError, match="invalid JSON evidence"):
        rehearsal._json_evidence(invalid)

    failed = tmp_path / "failed.json"
    failed.write_text(json.dumps({"ok": False}), encoding="utf-8")
    with pytest.raises(rehearsal.RehearsalError, match="not passing"):
        rehearsal._json_evidence(failed)

    with pytest.raises(rehearsal.RehearsalError, match="missing"):
        rehearsal._evidence_ref(tmp_path / "missing.json")


def test_run_binds_backup_clean_restore_and_material_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    env = _env()
    monkeypatch.setattr(rehearsal, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(
        rehearsal,
        "_tool_version",
        lambda binary, env: f"{binary} (PostgreSQL) 17.5",
    )
    identities = iter(
        [
            ("source", "170005", 8),
            ("restore", "170005", 0),
        ]
    )
    monkeypatch.setattr(
        rehearsal, "_query_identity", lambda *args, **kwargs: next(identities)
    )

    def fake_fingerprint(*, output: Path, expected_name: str, **kwargs: object) -> None:
        document = {
            "format": rehearsal.FINGERPRINT_FORMAT,
            "ok": True,
            "actual_schema_head": "0038_head",
            "database_name": expected_name,
            "database_fingerprint_sha256": "b" * 64,
            "tables": [{"table": "public.tasks", "row_count": 8}],
        }
        output.write_text(json.dumps(document), encoding="utf-8")

    monkeypatch.setattr(rehearsal, "_fingerprint", fake_fingerprint)

    def fake_compare(*, output: Path, **kwargs: object) -> None:
        output.write_text(
            json.dumps(
                {
                    "format": rehearsal.COMPARISON_FORMAT,
                    "ok": True,
                    "report_sha256": "c" * 64,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(rehearsal, "_compare", fake_compare)

    executed: list[list[str | Path]] = []

    def fake_run(
        command: list[str | Path], *, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        executed.append(list(command))
        if str(command[0]) == "pg_dump":
            output_index = command.index("--file") + 1
            Path(command[output_index]).write_bytes(b"custom-dump")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(rehearsal, "_run", fake_run)
    def fake_copy(backup: Path, destination: Path) -> dict[str, object]:
        destination.write_bytes(backup.read_bytes())
        return {
            "path": str(destination),
            "sha256": rehearsal._sha256(backup),
            "size_bytes": backup.stat().st_size,
            "source_device": 1,
            "retention_device": 2,
            "independent_device": True,
        }

    monkeypatch.setattr(rehearsal, "_copy_off_device", fake_copy)

    report = rehearsal.run(args, environ=env)

    assert report["ok"] is True
    assert report["checkout_commit"] == "a" * 40
    assert report["backup"]["sha256"] == rehearsal._sha256(
        args.output_dir / "postgresql-authority.dump"
    )
    assert report["restore_target"]["initial_public_table_count"] == 0
    assert report["off_device_retention"]["independent_device"] is True
    assert report["source"]["libpq_target"] == "derived_from_canonical_database_url"
    assert report["restore_target"]["libpq_target"] == "derived_from_canonical_database_url"
    dump_command = next(command for command in executed if str(command[0]) == "pg_dump")
    restore_command = next(command for command in executed if str(command[0]) == "pg_restore")
    assert str(dump_command[-1]) == "postgresql://source:secret@db/rehearsal"
    assert str(restore_command[restore_command.index("--dbname") + 1]) == (
        "postgresql://restore:secret@db/restore"
    )
    assert report["verification"]["database_fingerprint_sha256"] == "b" * 64
    persisted = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    persisted_sha256 = persisted.pop("report_sha256")
    assert persisted_sha256 == hashlib.sha256(
        rehearsal._canonical(persisted)
    ).hexdigest()


def test_restore_mismatch_fails_closed_and_keeps_backup_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    env = _env()
    monkeypatch.setattr(rehearsal, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(rehearsal, "_tool_version", lambda *args, **kwargs: "PostgreSQL 17.5")
    identities = iter([("source", "170005", 1), ("restore", "170005", 0)])
    monkeypatch.setattr(rehearsal, "_query_identity", lambda *args, **kwargs: next(identities))
    fingerprints = iter(["b" * 64, "b" * 64, "d" * 64])

    def fake_fingerprint(*, output: Path, expected_name: str, **kwargs: object) -> None:
        output.write_text(
            json.dumps(
                {
                    "format": rehearsal.FINGERPRINT_FORMAT,
                    "ok": True,
                    "actual_schema_head": "0038_head",
                    "database_name": expected_name,
                    "database_fingerprint_sha256": next(fingerprints),
                    "tables": [{"table": "public.tasks", "row_count": 1}],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(rehearsal, "_fingerprint", fake_fingerprint)

    comparisons = iter([True, False])

    def fake_compare(*, output: Path, **kwargs: object) -> None:
        ok = next(comparisons)
        output.write_text(
            json.dumps(
                {
                    "format": rehearsal.COMPARISON_FORMAT,
                    "ok": ok,
                    "report_sha256": "c" * 64,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(rehearsal, "_compare", fake_compare)

    def fake_run(
        command: list[str | Path], *, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        if str(command[0]) == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"custom-dump")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(rehearsal, "_run", fake_run)
    monkeypatch.setattr(
        rehearsal,
        "_copy_off_device",
        lambda backup, destination: {
            "path": str(destination),
            "sha256": rehearsal._sha256(backup),
            "size_bytes": backup.stat().st_size,
            "source_device": 1,
            "retention_device": 2,
            "independent_device": True,
        },
    )

    with pytest.raises(rehearsal.RehearsalError, match="evidence is not passing"):
        rehearsal.run(args, environ=env)

    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    assert report["ok"] is False
    assert report["backup"]["sha256"] == rehearsal._sha256(
        args.output_dir / "postgresql-authority.dump"
    )
    assert report["off_device_retention"]["independent_device"] is True
    assert report["report_sha256"]


def test_artifact_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"changed")
    with pytest.raises(rehearsal.RehearsalError, match="backup artifact SHA-256 mismatch"):
        rehearsal._require_sha256(backup, "0" * 64, label="backup artifact")


def test_output_directory_must_be_empty(tmp_path: Path) -> None:
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "stale.json").write_text("stale", encoding="utf-8")
    with pytest.raises(rehearsal.RehearsalError, match="must be empty"):
        rehearsal._prepare_output_dir(output)


def test_production_shaped_source_requires_material_authority_rows() -> None:
    document = {
        "format": rehearsal.FINGERPRINT_FORMAT,
        "database_name": "source",
        "actual_schema_head": "0038_head",
        "database_fingerprint_sha256": "b" * 64,
        "tables": [{"table": "public.tasks", "row_count": 0}],
    }
    with pytest.raises(rehearsal.RehearsalError, match="no material authority rows"):
        rehearsal._fingerprint_evidence(
            document,
            expected_database_name="source",
            expected_schema_head="0038_head",
            require_material_rows=True,
        )


def test_nonempty_restore_target_fails_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    env = _env()
    monkeypatch.setattr(rehearsal, "_git_head", lambda *args, **kwargs: "a" * 40)
    monkeypatch.setattr(rehearsal, "_tool_version", lambda *args, **kwargs: "PostgreSQL 17.5")
    identities = iter([("source", "170005", 5), ("restore", "170005", 1)])
    monkeypatch.setattr(rehearsal, "_query_identity", lambda *args, **kwargs: next(identities))

    with pytest.raises(rehearsal.RehearsalError, match="restore target is not clean"):
        rehearsal.run(args, environ=env)

    assert not (args.output_dir / "postgresql-authority.dump").exists()
    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    assert report["ok"] is False
    assert report["report_sha256"]


def test_source_commit_mismatch_fails_before_database_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = _args(tmp_path)
    env = _env()
    monkeypatch.setattr(rehearsal, "_git_head", lambda *args, **kwargs: "b" * 40)

    with pytest.raises(rehearsal.RehearsalError, match="source commit mismatch"):
        rehearsal.run(args, environ=env)

    report = json.loads((args.output_dir / "rehearsal-report.json").read_text())
    assert report["ok"] is False
    assert "source" not in report
