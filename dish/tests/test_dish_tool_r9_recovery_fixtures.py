import json
import shutil
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upgrade"

from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import content_identity, initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.step9 import recover_operation

DB_NAME = "dish-tool-recovery-v12.sqlite"


class SidecarBackend:
    def __init__(self, tasks):
        self.tasks = {item["task_gid"]: dict(item) for item in tasks}

    def read_task(self, gid):
        item = self.tasks[gid]
        return {
            "gid": gid,
            "name": item["title"],
            "notes": item["notes"],
            "completed": False,
            "modified_at": "fixture",
            "projects": [{"gid": COOKING_PROJECT_GID}],
            "memberships": [{"project": {"gid": COOKING_PROJECT_GID}, "section": {"gid": item["section_gid"]}}],
        }

    def update_task_content(self, *, task_gid, title, notes):
        self.tasks[task_gid]["title"] = title
        self.tasks[task_gid]["notes"] = notes

    def move_task_to_section(self, *, task_gid, section_gid):
        self.tasks[task_gid]["section_gid"] = section_gid

    def list_sections(self, project_gid):
        return [
            {"gid": "research", "name": "Research Queue"},
            {"gid": "verification", "name": "Verification Queue"},
            {"gid": "destination", "name": "Destination"},
            {"gid": "third-section", "name": "Other"},
            {"gid": "ref", "name": "Reference"},
            {"gid": "src", "name": "Sourcing"},
        ]


def _semantic_snapshot(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    snapshot = {}
    for table in tables:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        order = ",".join(columns)
        rows = [tuple(row[col] for col in columns) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        snapshot[table] = {"columns": columns, "rows": rows}
    conn.close()
    return snapshot


def test_recovery_fixture_generator_is_reproducible(tmp_path):
    import runpy
    namespace = runpy.run_path(str(FIXTURES / "generate_recovery_fixtures.py"))
    namespace["build"](tmp_path)
    generated_db = tmp_path / DB_NAME
    assert _semantic_snapshot(generated_db) == _semantic_snapshot(FIXTURES / DB_NAME)
    assert (tmp_path / "live-tasks.json").read_bytes() == (FIXTURES / "live-tasks.json").read_bytes()
    assert (tmp_path / "fixture-matrix.json").read_bytes() == (FIXTURES / "fixture-matrix.json").read_bytes()


def test_recovery_fixture_identities_and_live_sidecars_are_truthful():
    conn = sqlite3.connect(FIXTURES / DB_NAME)
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
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_recovery_sidecars_execute_and_match_exact_row_diff_contracts(tmp_path):
    db_copy = tmp_path / "recovery.sqlite"
    shutil.copy2(FIXTURES / DB_NAME, db_copy)
    sidecar = json.loads((FIXTURES / "live-tasks.json").read_text())
    backend = SidecarBackend(sidecar["tasks"])
    conn = initialize_database(db_copy)

    for item in sidecar["tasks"]:
        op = conn.execute("SELECT operation_id FROM operations WHERE task_gid=?", (item["task_gid"],)).fetchone()[0]
        expected = item["expected_recovery"]
        contract = item.get("expected_row_diff")
        if contract:
            id_col = "attempt_id"
            before = conn.execute(f"SELECT {contract['column']} FROM {contract['table']} WHERE {id_col}=?", (contract["id"],)).fetchone()[0]
            assert before == contract["before"]

        if expected == "applied":
            result = recover_operation(conn, backend, operation_id=op, requested_outcome="applied", reason="fixture acceptance")
            assert result["actions"]
        elif expected == "not_applied":
            result = recover_operation(conn, backend, operation_id=op, requested_outcome="not-applied", reason="fixture acceptance")
            assert result["actions"]
        elif expected == "uncertain":
            result = recover_operation(conn, backend, operation_id=op, requested_outcome="inspect", reason="fixture acceptance")
            assert not result["actions"]
            for decision in ("applied", "not-applied"):
                with pytest.raises(DishRuleError):
                    recover_operation(conn, backend, operation_id=op, requested_outcome=decision, reason="contradictory fixture decision")
        else:
            # Closed and held scenarios must remain stable under inspection.
            recover_operation(conn, backend, operation_id=op, requested_outcome="inspect", reason="fixture acceptance")

        contradictory = item.get("contradictory_request")
        if contradictory:
            # Use a fresh disposable copy so the correct reconciliation above
            # does not hide the contradiction check.
            contradiction_db = tmp_path / f"contradiction-{item['task_gid']}.sqlite"
            shutil.copy2(FIXTURES / DB_NAME, contradiction_db)
            contradiction_conn = initialize_database(contradiction_db)
            with pytest.raises(DishRuleError) as exc:
                recover_operation(contradiction_conn, backend, operation_id=op, requested_outcome=contradictory, reason="must fail closed")
            assert exc.value.rule == "recovery_outcome_mismatch"
            contradiction_conn.close()

        if contract:
            after = conn.execute(f"SELECT {contract['column']} FROM {contract['table']} WHERE attempt_id=?", (contract["id"],)).fetchone()[0]
            assert after == contract["after"]

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
        "checked-in movement ambiguity",
        "contradictory recovery decisions",
        "partially finalized attempt",
        "exact row diff",
    }
    assert required <= coverage
