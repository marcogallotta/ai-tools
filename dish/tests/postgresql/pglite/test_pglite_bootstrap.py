"""PGlite bootstrap coverage through the shared TCP fixture."""
from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.pglite


def test_pglite_starts_and_reports_postgresql_version(pglite) -> None:
    with psycopg.connect(pglite.libpq_dsn) as connection:
        version = connection.execute("SELECT version()").fetchone()[0]
    assert "PostgreSQL" in version
    assert "PGlite" in version
