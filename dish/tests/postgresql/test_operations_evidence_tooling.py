from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dish_pg.bootstrap import DEFAULT_SCHEMA_HEAD
from dish_pg.operations_evidence import (
    FINGERPRINT_FORMAT,
    OperationsEvidenceError,
    compare_fingerprint_reports,
    validate_legacy_writer_inventory,
)
from dish_pg.release import ALEMBIC_HEAD

ROOT = Path(__file__).resolve().parents[2]


def _owner_only(path: Path, payload: bytes) -> tuple[str, str]:
    path.write_bytes(payload)
    path.chmod(0o600)
    return str(path.resolve()), hashlib.sha256(payload).hexdigest()


def _fingerprint(path: Path, *, digest: str, row_count: int = 1) -> None:
    report = {
        "format": FINGERPRINT_FORMAT,
        "status": "pass",
        "ok": True,
        "database_url_env": "DISH_PG_URL",
        "database_name": path.stem,
        "server_version_num": "170000",
        "expected_schema_head": ALEMBIC_HEAD,
        "actual_schema_head": ALEMBIC_HEAD,
        "tables": [
            {
                "table": "public.example",
                "columns": ["id"],
                "primary_key": ["id"],
                "row_count": row_count,
                "rows_sha256": digest,
            }
        ],
        "database_fingerprint_sha256": digest,
        "report_sha256": "f" * 64,
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def test_bootstrap_default_tracks_current_release_head() -> None:
    assert DEFAULT_SCHEMA_HEAD == ALEMBIC_HEAD == "0038_cutover_rehearsal_identity"


def test_database_fingerprint_comparison_is_machine_checkable(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    restored = tmp_path / "restored.json"
    _fingerprint(source, digest="a" * 64)
    _fingerprint(restored, digest="a" * 64)

    matched = compare_fingerprint_reports(source_path=source, restored_path=restored)
    assert matched["ok"] is True
    assert matched["differences"] == []

    _fingerprint(restored, digest="b" * 64, row_count=2)
    mismatch = compare_fingerprint_reports(source_path=source, restored_path=restored)
    assert mismatch["ok"] is False
    assert mismatch["differences"][0]["table"] == "public.example"


def test_legacy_writer_inventory_requires_all_scopes_and_matching_artifacts(tmp_path: Path) -> None:
    categories = []
    for kind, state in (
        ("process", "stopped"),
        ("endpoint", "blocked"),
        ("credential", "revoked"),
        ("scheduler", "disabled"),
    ):
        discovery_path, discovery_sha = _owner_only(
            tmp_path / f"{kind}-discovery.txt", f"discovered {kind}\n".encode()
        )
        evidence_path, evidence_sha = _owner_only(
            tmp_path / f"{kind}-closure.txt", f"closed {kind}\n".encode()
        )
        categories.append(
            {
                "kind": kind,
                "applicable": True,
                "discovery_evidence_path": discovery_path,
                "discovery_evidence_sha256": discovery_sha,
                "writers": [
                    {
                        "writer_id": f"legacy-{kind}-1",
                        "identity": f"fixture:{kind}:1",
                        "state": state,
                        "evidence_path": evidence_path,
                        "evidence_sha256": evidence_sha,
                    }
                ],
            }
        )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "format": "dish-legacy-writer-inventory-v1",
                "candidate_id": "10000000-0000-4000-8000-000000000001",
                "cutover_run_id": "10000000-0000-4000-8000-000000000002",
                "source_commit": "a" * 40,
                "categories": categories,
            }
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)

    report = validate_legacy_writer_inventory(inventory_path=inventory)
    assert report["ok"] is True
    assert report["writer_count"] == 4
    assert [item["kind"] for item in report["categories"]] == [
        "process",
        "endpoint",
        "credential",
        "scheduler",
    ]

    categories.pop()
    inventory.write_text(
        json.dumps(
            {
                "format": "dish-legacy-writer-inventory-v1",
                "candidate_id": "10000000-0000-4000-8000-000000000001",
                "cutover_run_id": "10000000-0000-4000-8000-000000000002",
                "source_commit": "a" * 40,
                "categories": categories,
            }
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)
    with pytest.raises(OperationsEvidenceError, match="missing categories: scheduler"):
        validate_legacy_writer_inventory(inventory_path=inventory)


def test_legacy_writer_inventory_binds_expected_cutover_identity(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "format": "dish-legacy-writer-inventory-v1",
                "candidate_id": "10000000-0000-4000-8000-000000000001",
                "cutover_run_id": "10000000-0000-4000-8000-000000000002",
                "source_commit": "a" * 40,
                "categories": [],
            }
        ),
        encoding="utf-8",
    )
    inventory.chmod(0o600)

    with pytest.raises(OperationsEvidenceError, match="expected candidate"):
        validate_legacy_writer_inventory(
            inventory_path=inventory,
            expected_candidate_id="10000000-0000-4000-8000-000000000099",
        )


def test_operations_evidence_entry_points_expose_current_flags() -> None:
    for command in (
        [sys.executable, "scripts/dish-pg-operations-evidence", "--help"],
        [sys.executable, "scripts/dish-pg-operations-evidence", "database-fingerprint", "--help"],
        [sys.executable, "scripts/dish-pg-operations-evidence", "compare-database-fingerprints", "--help"],
        [sys.executable, "scripts/dish-pg-operations-evidence", "validate-legacy-writer-inventory", "--help"],
        [sys.executable, "scripts/dish-pg-first-admission-request", "--help"],
    ):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=os.environ.copy(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout
