"""Committed §3 PostgreSQL runtime-wiring rehearsal across real processes."""
from __future__ import annotations

import pytest

from tests.support.postgresql.runtime_wiring_rehearsal import (
    run_runtime_wiring_rehearsal,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_runtime_wiring_rehearsal_across_service_and_worker_processes(
    core_db, tmp_path
) -> None:
    evidence = run_runtime_wiring_rehearsal(core_db, tmp_path)
    assert evidence["same_logical_worker_restart"]["status"] == "passed"
    assert evidence["different_worker_takeover"]["status"] == "passed"
    assert evidence["stale_original_worker_rejection"]["status"] == "passed"
    assert evidence["external_attempt_settlement"]["status"] == "passed"
    assert evidence["unsupported_test_service_routes"]["status"] == "passed"
    assert evidence["postgresql_loss"]["request_absent_after_restart"] is True
    assert evidence["postgresql_loss"]["task_absent_after_restart"] is True
