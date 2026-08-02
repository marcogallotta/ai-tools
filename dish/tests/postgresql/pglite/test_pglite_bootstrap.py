"""Smoke test proving the pglite offline dependency lane actually starts and is queryable.

This is not PostgreSQL certification evidence (see database-backend-imp.md). It only proves the
py-pglite + @electric-sql/pglite install in this repo is wired correctly.
"""

from pathlib import Path

import psycopg
import pytest

from py_pglite import PGliteConfig
from py_pglite import PGliteManager

WORK_DIR = Path(__file__).parent


@pytest.mark.pglite
def test_pglite_starts_and_reports_postgresql_version():
    # py-pglite reuses work_dir/pglite_manager.js across runs if present, but the script bakes in
    # a fresh per-process socket path each time — a stale script listens on a socket nothing is
    # waiting on. Force regeneration so the script always matches this process's socket path.
    generated_manager_js = WORK_DIR / "pglite_manager.js"
    generated_manager_js.unlink(missing_ok=True)

    config = PGliteConfig(work_dir=WORK_DIR)
    with PGliteManager(config) as manager:
        with psycopg.connect(manager.get_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                (version,) = cur.fetchone()

    assert "PostgreSQL" in version
