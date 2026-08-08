from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from dish_service.command_spec import ACTION_COMMANDS
from dish_tool.admin_command_spec import ADMIN_COMMANDS
from dish_tool.database_migrations import migrate_database

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "docs/database-backend-stage-a-baseline.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _sqlite_tables() -> list[str]:
    # Deliberately independent from the baseline generator: this test is the
    # oracle for the generated table inventory, so sharing its query/helper
    # would make the check self-confirming.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        migrate_database(conn)
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        return sorted(name for name in names if not name.startswith("sqlite_"))
    finally:
        conn.close()


def test_frozen_command_inventory_matches_current_surfaces() -> None:
    baseline = _baseline()

    assert list(ACTION_COMMANDS) == baseline["action_commands"]
    assert sorted(ADMIN_COMMANDS) == sorted(baseline["admin_commands"])

    expected_treatments = set(baseline["action_commands"]) | set(baseline["admin_commands"])
    expected_treatments.add("planning-intent-settlement")
    treatments = baseline["target_treatments"]
    source_only = baseline["source_only_commands"]
    assert isinstance(treatments, dict)
    assert isinstance(source_only, list)
    assert not (set(treatments) & set(source_only))
    assert set(treatments) | set(source_only) == expected_treatments
    assert all(
        re.fullmatch(r"(?:retain|retire|add):[A-Z]", treatment)
        for treatment in treatments.values()
    )


def test_frozen_sqlite_authority_inventory_matches_schema() -> None:
    assert _sqlite_tables() == _baseline()["sqlite_tables"]


def test_frozen_governing_sources_have_exact_hashes() -> None:
    baseline = _baseline()
    actual = {
        relative: _sha256(ROOT / relative)
        for relative in baseline["governing_source_sha256"]
    }
    assert actual == baseline["governing_source_sha256"]


def test_stage_a_baseline_does_not_freeze_test_file_hashes() -> None:
    assert "characterization_test_sha256" not in _baseline()


def test_stage_a_baseline_governs_canonical_admin_cli() -> None:
    sources = _baseline()["governing_source_sha256"]
    assert "dish_service/admin_cli.py" in sources
    assert "dish_tool/admin_cli.py" not in sources


def test_canonical_stage_a_regeneration_matches_checked_in_bytes(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = ROOT / "scripts/dish-pg-stage-a-baseline"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for output in (first, second):
        completed = subprocess.run(
            [sys.executable, str(script), "--output", str(output)],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout
    assert first.read_bytes() == second.read_bytes() == BASELINE_PATH.read_bytes()

    checked = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert checked.returncode == 0, checked.stdout
    assert "matches canonical regeneration" in checked.stdout


def test_governed_stage_a_write_requires_a_reason() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dish-pg-stage-a-baseline"), "--write"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 2
    assert "--reason is required" in completed.stdout
