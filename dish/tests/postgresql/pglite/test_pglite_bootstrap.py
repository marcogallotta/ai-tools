"""Smoke test proving the pglite offline dependency lane actually starts and is queryable.

This is not PostgreSQL certification evidence (see database-backend-imp.md). It only proves the
py-pglite + @electric-sql/pglite install in this repo is wired correctly.
"""

import socket
from pathlib import Path

import psycopg
import pytest

from py_pglite import PGliteConfig
from py_pglite import PGliteManager

WORK_DIR = Path(__file__).parent


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.pglite
def test_pglite_starts_and_reports_postgresql_version():
    # py-pglite reuses work_dir/pglite_manager.js across runs if present, but the script bakes in
    # a fresh per-process socket/port each time — a stale script listens on an address nothing is
    # waiting on. Force regeneration so the script always matches this process's address.
    generated_manager_js = WORK_DIR / "pglite_manager.js"
    generated_manager_js.unlink(missing_ok=True)

    # TCP rather than a Unix socket: some sandboxes (e.g. the network-restricted environment
    # ChatGPT develops in) close the psycopg connection immediately over a Unix socket but work
    # reliably over loopback TCP.
    config = PGliteConfig(
        work_dir=WORK_DIR,
        use_tcp=True,
        tcp_host="127.0.0.1",
        tcp_port=_free_tcp_port(),
    )
    with PGliteManager(config) as manager:
        with psycopg.connect(manager.get_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                (version,) = cur.fetchone()

    assert "PostgreSQL" in version
