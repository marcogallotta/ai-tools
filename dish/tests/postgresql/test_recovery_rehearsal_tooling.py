from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from dish_pg import models
from dish_pg import stage3_models as workflow_models
from dish_pg import stage5_models as projection_models
from dish_pg import stage6_models as release_models
from dish_pg.recovery_rehearsal import (
    BackupEvidence,
    CommandTimeout,
    InjectedRestoreFault,
    RehearsalError,
    Runner,
    _backup_evidence,
    _copy_backup_with_interruption,
    _interrupted_backup_fault,
    _record_bundle,
    _run_backup_reconcile_process,
    _seed_baseline,
    _source_manifest,
    _verify_backup,
    _write_archive_helpers,
    _write_reservation,
    finalize_backup,
    main,
    require_empty_target,
)

SYSTEM_ID = "7600000000000000000"


def _fake_backup(path: Path, *, marker: str = "original") -> None:
    (path / "global").mkdir(parents=True)
    (path / "base").mkdir()
    (path / "PG_VERSION").write_text("17\n", encoding="utf-8")
    manifest = {
        "PostgreSQL-Backup-Manifest-Version": 2,
        "WAL-Ranges": [
            {"Timeline": 1, "Start-LSN": "0/100", "End-LSN": "0/200"}
        ],
        "marker": marker,
    }
    (path / "backup_manifest").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )


def _verify_fake(path: Path) -> BackupEvidence:
    required = (path / "PG_VERSION", path / "backup_manifest", path / "global", path / "base")
    if not all(item.exists() for item in required):
        raise RehearsalError("incomplete fake backup")
    return _backup_evidence(path, system_identifier=SYSTEM_ID)


def _fake_pg_verifybackup(path: Path) -> Path:
    executable = path / "pg_verifybackup"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _fake_pg_controldata(path: Path, *, system_identifier: str = SYSTEM_ID) -> Path:
    executable = path / f"pg_controldata_{system_identifier}"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf 'Database system identifier: {system_identifier}\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _reconcile_request(tmp_path: Path, parent: Path) -> Path:
    request = parent / "request.json"
    request.write_text(
        json.dumps(
            {
                "parent": str(parent),
                "candidate": str(parent / "candidate"),
                "final": str(parent / "final"),
                "system_identifier": SYSTEM_ID,
                "pg_verifybackup": str(_fake_pg_verifybackup(tmp_path)),
                "pg_controldata": str(_fake_pg_controldata(tmp_path)),
                "log_dir": str(tmp_path / "child-logs"),
            }
        ),
        encoding="utf-8",
    )
    return request


def test_backup_rename_recovery_crosses_real_process_boundary(tmp_path, monkeypatch):
    parent = tmp_path / "backup"
    parent.mkdir()
    candidate, final = parent / "candidate", parent / "final"
    _fake_backup(candidate)
    _write_reservation(
        parent, candidate=candidate, final=final, system_identifier=SYSTEM_ID
    )
    request = _reconcile_request(tmp_path, parent)
    runner = Runner(tmp_path / "parent-logs")
    monkeypatch.chdir(tmp_path)
    first, first_result = _run_backup_reconcile_process(
        runner,
        request_path=request,
        result_path=parent / "first.json",
        inject_after_rename=True,
    )
    assert first.returncode == 75
    assert first_result["status"] == "interrupted_after_rename"
    assert first_result["commands"]
    assert final.exists() and not candidate.exists()
    second, second_result = _run_backup_reconcile_process(
        runner,
        request_path=request,
        result_path=parent / "second.json",
        inject_after_rename=False,
    )
    assert second.returncode == 0
    assert second_result["status"] == "passed"
    assert first_result["pid"] != second_result["pid"]
    first_log = Path(first_result["commands"][0]["stdout_log"])
    second_log = Path(second_result["commands"][0]["stdout_log"])
    assert first_log != second_log
    assert first_log.exists() and second_log.exists()
    assert sum(item.argv[0].endswith("pg_verifybackup") for item in runner.commands) == 2
    reservation = json.loads((parent / "backup-reservation.json").read_text())
    assert reservation["state"] == "finalized"
    assert reservation["backup_evidence"] == second_result["backup_evidence"]


def test_restart_reconciliation_refuses_stale_result_file(tmp_path):
    parent = tmp_path / "backup"
    parent.mkdir()
    candidate, final = parent / "candidate", parent / "final"
    _fake_backup(candidate)
    _write_reservation(
        parent, candidate=candidate, final=final, system_identifier=SYSTEM_ID
    )
    result_path = parent / "result.json"
    result_path.write_text('{"status":"passed"}\n', encoding="utf-8")
    with pytest.raises(RehearsalError, match="refusing stale evidence"):
        _run_backup_reconcile_process(
            Runner(tmp_path / "parent-logs"),
            request_path=_reconcile_request(tmp_path, parent),
            result_path=result_path,
            inject_after_rename=False,
        )


def test_backup_verification_reads_system_identifier_from_control_data(tmp_path):
    backup = tmp_path / "backup"
    _fake_backup(backup)
    runner = Runner(tmp_path / "logs")
    wrong_system_identifier = "7600000000000000001"
    binaries = {
        "pg_verifybackup": _fake_pg_verifybackup(tmp_path),
        "pg_controldata": _fake_pg_controldata(
            tmp_path, system_identifier=wrong_system_identifier
        ),
    }
    with pytest.raises(RehearsalError, match="different PostgreSQL system identifier"):
        _verify_backup(runner, binaries, backup, system_identifier=SYSTEM_ID)
    assert [Path(item.argv[0]).name for item in runner.commands] == [
        "pg_verifybackup",
        f"pg_controldata_{wrong_system_identifier}",
    ]


def test_restart_reconciliation_rejects_changed_final_output(tmp_path):
    parent = tmp_path / "backup"
    parent.mkdir()
    candidate, final = parent / "candidate", parent / "final"
    _fake_backup(candidate)
    _write_reservation(
        parent, candidate=candidate, final=final, system_identifier=SYSTEM_ID
    )
    request = _reconcile_request(tmp_path, parent)
    runner = Runner(tmp_path / "parent-logs")
    first, _ = _run_backup_reconcile_process(
        runner,
        request_path=request,
        result_path=parent / "first.json",
        inject_after_rename=True,
    )
    assert first.returncode == 75
    manifest = json.loads((final / "backup_manifest").read_text(encoding="utf-8"))
    manifest["marker"] = "changed-after-restart"
    (final / "backup_manifest").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    second, result = _run_backup_reconcile_process(
        runner,
        request_path=request,
        result_path=parent / "second.json",
        inject_after_rename=False,
    )
    assert second.returncode == 2
    assert result["status"] == "failed"
    assert "evidence" in result["error"] or "backup" in result["error"]


@pytest.mark.parametrize("state", ["partial", "ambiguous", "stale"])
def test_restart_reconciliation_rejects_invalid_existing_state(tmp_path, state):
    parent = tmp_path / state
    parent.mkdir()
    candidate, final = parent / "candidate", parent / "final"
    if state in {"ambiguous", "stale"}:
        _fake_backup(candidate)
    if state == "ambiguous":
        _fake_backup(final)
    _write_reservation(
        parent,
        candidate=candidate,
        final=final,
        system_identifier="wrong-system" if state == "stale" else SYSTEM_ID,
    )
    completed, result = _run_backup_reconcile_process(
        Runner(tmp_path / "parent-logs"),
        request_path=_reconcile_request(tmp_path, parent),
        result_path=parent / "result.json",
        inject_after_rename=False,
    )
    assert completed.returncode == 2
    assert result["status"] == "failed"


def test_finalized_reservation_rejects_changed_or_conflicting_output(tmp_path):
    parent = tmp_path / "backup"
    parent.mkdir()
    candidate, final = parent / "candidate", parent / "final"
    _fake_backup(candidate)
    evidence = finalize_backup(
        parent,
        candidate=candidate,
        final=final,
        system_identifier=SYSTEM_ID,
        verifier=_verify_fake,
    )
    assert evidence.manifest_sha256 == _verify_fake(final).manifest_sha256
    manifest = json.loads((final / "backup_manifest").read_text(encoding="utf-8"))
    manifest["marker"] = "changed"
    (final / "backup_manifest").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(RehearsalError, match="evidence does not match"):
        finalize_backup(
            parent,
            candidate=candidate,
            final=final,
            system_identifier=SYSTEM_ID,
            verifier=_verify_fake,
        )


def test_backup_reservation_ambiguity_partial_and_stale_fail_closed(tmp_path):
    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    candidate, final = ambiguous / "candidate", ambiguous / "final"
    _fake_backup(candidate)
    _fake_backup(final)
    with pytest.raises(RehearsalError, match="ambiguous"):
        finalize_backup(
            ambiguous, candidate=candidate, final=final,
            system_identifier=SYSTEM_ID, verifier=_verify_fake,
        )
    partial = tmp_path / "partial"
    partial.mkdir()
    with pytest.raises(RehearsalError, match="neither candidate nor final"):
        finalize_backup(
            partial, candidate=partial / "candidate", final=partial / "final",
            system_identifier=SYSTEM_ID, verifier=_verify_fake,
        )
    stale = tmp_path / "stale"
    stale.mkdir()
    _fake_backup(stale / "candidate")
    _write_reservation(
        stale, candidate=stale / "candidate", final=stale / "final",
        system_identifier="wrong-system",
    )
    with pytest.raises(RehearsalError, match="stale backup reservation"):
        finalize_backup(
            stale, candidate=stale / "candidate", final=stale / "final",
            system_identifier=SYSTEM_ID, verifier=_verify_fake,
        )


def test_runner_timeout_terminates_process_group_and_preserves_logs(tmp_path):
    marker = tmp_path / "grandchild-finished"
    code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"\"import time,pathlib; time.sleep(0.8); pathlib.Path(r'{marker}').write_text('bad')\"]); "
        "time.sleep(30)"
    )
    runner = Runner(tmp_path / "logs")
    with pytest.raises(CommandTimeout, match="timed out"):
        runner.run([sys.executable, "-c", code], timeout_seconds=0.1)
    time.sleep(1.0)
    assert not marker.exists()
    evidence = runner.commands[-1]
    assert evidence.timed_out is True
    assert evidence.termination in {"SIGTERM", "SIGTERM_then_SIGKILL"}
    assert Path(evidence.stdout_log).exists()
    assert Path(evidence.stderr_log).exists()


def test_interrupted_backup_requires_reaching_the_injection_boundary(tmp_path):
    executable = tmp_path / "pg_basebackup"
    executable.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    executable.chmod(0o755)
    runner = Runner(tmp_path / "logs")
    source = SimpleNamespace(
        binaries={"pg_basebackup": executable},
        port=56520,
        runner=runner,
        system_identifier=lambda: SYSTEM_ID,
    )

    with pytest.raises(RehearsalError, match="exited before the deterministic injection"):
        _interrupted_backup_fault(source, tmp_path / "interrupted")

    evidence = runner.commands[-1]
    assert evidence.returncode == 3
    assert evidence.termination == "exited_before_injection"


def test_interrupted_backup_timeout_is_not_reported_as_injected_fault(tmp_path):
    executable = tmp_path / "pg_basebackup"
    executable.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    executable.chmod(0o755)
    runner = Runner(tmp_path / "logs")
    source = SimpleNamespace(
        binaries={"pg_basebackup": executable},
        port=56520,
        runner=runner,
        system_identifier=lambda: SYSTEM_ID,
    )

    with pytest.raises(CommandTimeout, match="before the deterministic injection"):
        _interrupted_backup_fault(
            source, tmp_path / "interrupted", timeout_seconds=0.1
        )

    evidence = runner.commands[-1]
    assert evidence.timed_out is True
    assert evidence.cleanup_result in {"terminated", "killed", "exited_during_escalation"}

def test_unexpected_and_interrupted_restore_outputs_are_rejected(tmp_path):
    unexpected = tmp_path / "unexpected"
    unexpected.mkdir()
    (unexpected / "foreign").write_text("state\n", encoding="utf-8")
    with pytest.raises(RehearsalError, match="unexpected state"):
        require_empty_target(unexpected)
    backup = tmp_path / "source"
    _fake_backup(backup)
    for index in range(12):
        (backup / "base" / f"file-{index:02d}").write_text(str(index))
    target = tmp_path / "target"
    with pytest.raises(InjectedRestoreFault, match="before restore finalization"):
        _copy_backup_with_interruption(backup, target, stop_after_files=4)
    with pytest.raises(RehearsalError, match="incomplete fake backup"):
        _verify_fake(target)


def test_archive_helpers_reject_conflicts_and_restore_exact_wal(tmp_path):
    archive_dir = tmp_path / "wal-archive"
    archive, restore = _write_archive_helpers(tmp_path / "helpers", archive_dir)
    source = tmp_path / "source-wal"
    source.write_bytes(b"first-wal-segment")
    wal_name = "000000010000000000000001"
    assert subprocess.run([archive, source, wal_name], timeout=5).returncode == 0
    assert subprocess.run([archive, source, wal_name], timeout=5).returncode == 0
    source.write_bytes(b"conflicting-wal-segment")
    assert subprocess.run([archive, source, wal_name], timeout=5).returncode != 0
    restored = tmp_path / "restored-wal"
    assert subprocess.run([restore, wal_name, restored], timeout=5).returncode == 0
    assert restored.read_bytes() == b"first-wal-segment"


def test_boundary_bundle_records_temporal_state_with_authorized_candidate(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'recovery-state.sqlite3'}", future=True
    )
    models.Base.metadata.create_all(engine)
    try:
        context = _seed_baseline(
            engine, tmp_path / "evidence", dish_commit="e" * 40
        )
        _record_bundle(engine, context, "boundary_a", 2)
        with engine.connect() as connection:
            outcome_labels = {
                row[0]["label"]
                for row in connection.execute(
                    select(workflow_models.ServiceRequestOutcome.result_payload)
                )
            }
            projection_labels = {
                row[0]["label"]
                for row in connection.execute(
                    select(projection_models.ProjectionOutboxEvent.intent_payload)
                )
            }
            release_labels = set(
                connection.scalars(
                    select(release_models.ReleaseEvidenceItem.evidence_key).where(
                        release_models.ReleaseEvidenceItem.category == "postgresql_recovery"
                    )
                )
            )
        assert {"baseline", "boundary_a"}.issubset(outcome_labels)
        assert {"baseline", "boundary_a"}.issubset(projection_labels)
        assert {"baseline", "boundary_a"}.issubset(release_labels)
    finally:
        engine.dispose()


def test_source_manifest_binds_recovery_code_and_configuration():
    manifest = _source_manifest()
    paths = {item["path"] for item in manifest["files"]}
    assert "dish_pg/recovery_control.py" in paths
    assert "dish_pg/recovery_rehearsal.py" in paths
    assert "dish_pg/workflow.py" in paths
    assert "dish_pg/candidate_manifest.py" in paths
    assert "dish_pg/release.py" in paths
    assert "dish_pg/stage6_models.py" in paths
    assert "deploy/postgresql/compose.yaml" in paths
    assert len(manifest["manifest_sha256"]) == 64
    assert all(item["size_bytes"] > 0 for item in manifest["files"])


def test_cli_writes_truthful_blocked_report_with_verified_source_identity(tmp_path):
    empty_pg_bin = tmp_path / "pg-bin"
    empty_pg_bin.mkdir()
    report = tmp_path / "report.json"
    status = main(
        [
            "--report", str(report),
            "--evidence-dir", str(tmp_path / "evidence"),
            "--work-root", str(tmp_path / "work"),
            "--pg-bin", str(empty_pg_bin),
        ]
    )
    assert status == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["source_identity"]["caller_identity_is_authoritative"] is False
    assert len(payload["source_identity"]["execution_identity"]) == 64
    assert isinstance(payload["source_identity"]["worktree_clean"], bool)
    assert isinstance(payload["source_identity"]["worktree_status"], list)
    assert (
        payload["source_identity"]["relevant_tree_identity"]
        == payload["source_identity"]["execution_identity"]
    )
    assert set(payload["blocked"]["missing_commands"]) >= {"initdb", "psql"}
    assert "physical backup and verification" in payload["blocked"]["remaining_native_scenarios"]
    assert "measurements" not in payload
    assert not (tmp_path / "work").exists()


def test_declared_synthetic_base_requires_verified_parent(tmp_path):
    report = tmp_path / "report.json"
    status = main(
        [
            "--report", str(report),
            "--evidence-dir", str(tmp_path / "evidence"),
            "--work-root", str(tmp_path / "work"),
            "--source-identity-kind", "synthetic_base",
        ]
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert status == 2
    assert payload["status"] == "failed"
    assert "requires --base-commit" in payload["error"]["message"]


def test_caller_commit_mismatch_is_rejected_before_native_claims(tmp_path):
    report = tmp_path / "report.json"
    status = main(
        [
            "--report", str(report),
            "--evidence-dir", str(tmp_path / "evidence"),
            "--work-root", str(tmp_path / "work"),
            "--dish-commit", "0" * 40,
        ]
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert status == 2
    assert payload["status"] == "failed"
    assert "does not match executed Git commit" in payload["error"]["message"]
