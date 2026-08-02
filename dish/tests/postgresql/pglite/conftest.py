"""Fixtures for the non-certification PGlite development lane."""
from __future__ import annotations

import pytest

from tests.support.postgresql.pglite import pglite_runtime


@pytest.fixture
def pglite():
    with pglite_runtime() as runtime:
        yield runtime
