from __future__ import annotations

import sqlite3

import pytest

from dish_tool.database_initialization import initialize_database


@pytest.mark.database_boundary
@pytest.mark.database_boundary_bootstrap
@pytest.mark.real_database_bootstrap
@pytest.mark.production_sqlite_pragmas
def test_database_boundary_lane_uses_real_bootstrap_and_production_pragmas(tmp_path):
    path = tmp_path / "boundary.sqlite"
    assert not path.exists()

    conn = initialize_database(path)
    try:
        assert path.exists()
        assert conn.execute("PRAGMA synchronous").fetchone()[0] != 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] > 0
    finally:
        conn.close()


def test_fast_database_lane_explicitly_disables_filesystem_sync(tmp_path):
    conn = sqlite3.connect(tmp_path / "fast.sqlite", isolation_level=None)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 0
    finally:
        conn.close()
