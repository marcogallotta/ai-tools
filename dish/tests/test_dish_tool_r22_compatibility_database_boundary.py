import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from dish_tool.database_schema import initialize_database
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


def _hold_writer(path: str, ready, release):
    conn = sqlite3.connect(path, timeout=1, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    ready.set()
    release.wait(10)
    conn.execute("ROLLBACK")
    conn.close()


def test_held_writer_returns_structured_retryable_error(tmp_path: Path):
    path = tmp_path / "writer.sqlite"
    initialize_database(path).close()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_writer, args=(str(path), ready, release))
    proc.start()
    assert ready.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(DishRuleError) as exc:
            initialize_database(path)
        assert exc.value.rule == "database_writer_lock"
        assert exc.value.retryable is True
        assert time.monotonic() - started < 5
    finally:
        release.set()
        proc.join(5)
