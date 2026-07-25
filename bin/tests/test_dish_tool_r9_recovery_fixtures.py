import json
import sqlite3
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upgrade"
sys.path.insert(0, str(BIN))

from dish_tool.database import content_identity


def test_recovery_fixture_identities_and_live_sidecars_are_truthful():
    conn = sqlite3.connect(FIXTURES / "dish-tool-recovery-v6.sqlite")
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT identity, title, notes FROM content_versions"):
        assert row["identity"] == content_identity(row["title"], row["notes"]).digest
    for row in conn.execute(
        "SELECT last_confirmed_identity, last_confirmed_title, last_confirmed_notes FROM task_content_state"
    ):
        assert row["last_confirmed_identity"] == content_identity(
            row["last_confirmed_title"], row["last_confirmed_notes"]
        ).digest
    for row in conn.execute(
        "SELECT intended_identity, intended_title, intended_notes FROM write_attempts WHERE intended_identity IS NOT NULL"
    ):
        assert row["intended_identity"] == content_identity(
            row["intended_title"], row["intended_notes"]
        ).digest

    sidecar = json.loads((FIXTURES / "live-tasks.json").read_text())
    tasks = {item["task_gid"]: item for item in sidecar["tasks"]}
    db_tasks = {row[0] for row in conn.execute("SELECT task_gid FROM operations")}
    assert set(tasks) == db_tasks
    for item in tasks.values():
        assert content_identity(item["title"], item["notes"]).digest
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_recovery_fixture_matrix_covers_release_gate_scenarios():
    matrix = json.loads((FIXTURES / "fixture-matrix.json").read_text())
    coverage = {term for scenario in matrix["scenarios"] for term in scenario["covers"]}
    required = {
        "started write",
        "live applied",
        "live not_applied",
        "uncertain write",
        "destination movement",
        "signed binding",
        "multiple content versions",
        "confirmed write",
        "not-applied attempt",
        "evidence review",
        "human_review review",
        "two-pass-hold",
    }
    assert required <= coverage
