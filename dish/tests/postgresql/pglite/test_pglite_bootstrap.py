"""PGlite bootstrap coverage through the shared TCP fixture."""
from __future__ import annotations

import psycopg
import pytest

pytestmark = pytest.mark.pglite


@pytest.mark.quarantined(
    issue="DISH-STAGE-A-PGLITE",
    owner="Marco",
    first_seen="2026-08-02",
    quarantined_on="2026-08-02",
    expires="2026-08-09",
    signature="server closed the connection unexpectedly during PGlite TCP startup under full-suite load",
)
def test_pglite_starts_and_reports_postgresql_version(pglite) -> None:
    with psycopg.connect(pglite.libpq_dsn) as connection:
        version = connection.execute("SELECT version()").fetchone()[0]
    assert "PostgreSQL" in version
    assert "PGlite" in version
