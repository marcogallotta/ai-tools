from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.history_backfill import apply_terminal_history_snapshot, capture_terminal_history_snapshot
from tests.postgresql.test_terminal_history_backfill import (
    SOURCE_COMMIT,
    TASK_GID,
    _insert_terminal_history,
    _legacy_db,
    _seed_target,
)
from tests.support.postgresql.core import core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_native_postgresql_terminal_history_backfill_is_atomic_and_idempotent(
    core_db, tmp_path: Path
) -> None:
    factory, _context, task_id, target = _seed_target(core_db)
    operation_id, cycle_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy,
        operation_id=operation_id,
        cycle_id=cycle_id,
        lease_id=lease_id,
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "terminal-history.ndjson",
    )

    with session_scope(factory) as session:
        first = apply_terminal_history_snapshot(
            session, target=target, snapshot=snapshot, source_commit=SOURCE_COMMIT
        )
        supplemental = first.supplemental_import_run_id
        assert supplemental is not None
        assert first.inserted_operations == 1
        assert first.inserted_verification_cycles == 1
        assert first.inserted_leases == 1

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        cycle = session.get(wf.VerificationCycle, cycle_id)
        lease = session.get(wf.ServiceLease, lease_id)
        assert operation is not None and operation.import_run_id == supplemental
        assert cycle is not None and cycle.import_run_id == supplemental
        assert lease is not None and lease.import_run_id == supplemental
        run = session.get(models.ImportRun, supplemental)
        assert run is not None and run.provenance["primary_import_run_id"] == str(
            target.primary_import_run_id
        )

        second = apply_terminal_history_snapshot(
            session, target=target, snapshot=snapshot, source_commit=SOURCE_COMMIT
        )
        assert second.supplemental_import_run_id == supplemental
        assert second.inserted_operations == 0
        assert second.inserted_verification_cycles == 0
        assert second.inserted_leases == 0
        assert int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0) == 2
