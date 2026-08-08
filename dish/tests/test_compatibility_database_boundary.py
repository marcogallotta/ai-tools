import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import dish_tool.database_initialization as database_initialization
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError


def test_current_ledger_must_be_contiguous(tmp_path: Path):
    path = tmp_path / "gap.sqlite"
    conn = initialize_database(path)
    conn.execute("DELETE FROM schema_migrations WHERE version=7")
    conn.close()
    with pytest.raises(DishRuleError) as exc:
        initialize_database(path)
    assert exc.value.rule == "database_ledger_gap"


@pytest.mark.parametrize(
    "sql,missing",
    [
        ("DROP TRIGGER write_attempt_confirmed_binding_update", "trigger:write_attempt_confirmed_binding_update"),
        ("DROP INDEX marco_authorizations_lookup_idx", "index:marco_authorizations_lookup_idx"),
    ],
)
def test_current_schema_manifest_detects_missing_objects(tmp_path: Path, sql: str, missing: str):
    path = tmp_path / "drift.sqlite"
    conn = initialize_database(path)
    conn.execute(sql)
    conn.close()
    with pytest.raises(DishRuleError) as exc:
        initialize_database(path)
    assert exc.value.rule == "database_schema_signature_mismatch"
    assert missing in exc.value.details["missing_objects"]


_HOLD_WRITER_SCRIPT = r"""
import sqlite3
import sys
import time
from pathlib import Path

ready_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
conn = sqlite3.connect(sys.argv[1], timeout=1, isolation_level=None)
try:
    conn.execute("BEGIN IMMEDIATE")
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release_path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("writer release signal was not received")
        time.sleep(0.01)
    conn.execute("ROLLBACK")
finally:
    conn.close()
"""


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_concurrency
@pytest.mark.boundary
def test_held_writer_returns_structured_retryable_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(database_initialization, "MIGRATION_BUSY_TIMEOUT_MS", 10)
    path = tmp_path / "writer.sqlite"
    ready_path = tmp_path / "writer.ready"
    release_path = tmp_path / "writer.release"
    initialize_database(path).close()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLD_WRITER_SCRIPT,
            str(path),
            str(ready_path),
            str(release_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    stderr = ""
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and proc.poll() is None:
            assert time.monotonic() < deadline, (
                "writer subprocess did not acquire the database lock"
            )
            time.sleep(0.01)
        assert ready_path.read_text(encoding="utf-8") == "ready"

        started = time.monotonic()
        with pytest.raises(DishRuleError) as exc:
            initialize_database(path)
        assert exc.value.rule == "database_writer_lock"
        assert exc.value.retryable is True
        assert time.monotonic() - started < 5
    finally:
        release_path.touch()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stderr is not None:
            stderr = proc.stderr.read()
            proc.stderr.close()

    assert proc.returncode == 0, stderr
