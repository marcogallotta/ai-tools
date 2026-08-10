from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.history_backfill import (
    TerminalHistoryBackfillError,
    apply_terminal_history_snapshot,
    capture_terminal_history_snapshot,
)
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


def _seed_candidate_bundle(core_db, factory, context, task_id):
    from datetime import timedelta

    from tests.support.postgresql.release import _prepare_candidate
    from tests.support.postgresql.workflow import NOW as RELEASE_NOW

    ids = core_db[1]
    with session_scope(factory) as session:
        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        bundle = service.build_evidence_bundle(
            candidate_id=candidate_id,
            bundle_kind="release_candidate",
            built_at=RELEASE_NOW,
        )
        return candidate_id, bundle.bundle_id, RELEASE_NOW + timedelta(minutes=1)


def _terminal_snapshot(tmp_path: Path, *, task_id: uuid.UUID, suffix: str):
    operation_id, cycle_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    legacy = tmp_path / f"legacy-{suffix}.sqlite3"
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
        output=tmp_path / f"terminal-history-{suffix}.ndjson",
    )
    return snapshot, operation_id


def test_native_postgresql_backfill_gate_first_serializes_candidate_validation(
    core_db, tmp_path: Path
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from dish_pg.release import ReleaseCandidateService
    from dish_pg import stage6_models as rel

    factory, context, task_id, target = _seed_target(core_db)
    candidate_id, bundle_id, validated_at = _seed_candidate_bundle(
        core_db, factory, context, task_id
    )
    snapshot, operation_id = _terminal_snapshot(
        tmp_path, task_id=task_id, suffix="backfill-first"
    )

    backfill_session = factory()
    validation_session = factory()
    try:
        result = apply_terminal_history_snapshot(
            backfill_session,
            target=target,
            snapshot=snapshot,
            source_commit=SOURCE_COMMIT,
        )
        assert result.supplemental_import_run_id is not None

        validation_session.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            ReleaseCandidateService(validation_session).validate_candidate(
                candidate_id=candidate_id,
                evidence_bundle_id=bundle_id,
                validated_at=validated_at,
            )
        validation_session.rollback()

        backfill_session.commit()
    finally:
        validation_session.close()
        backfill_session.close()

    with session_scope(factory) as session:
        ReleaseCandidateService(session).validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle_id,
            validated_at=validated_at,
        )

    with session_scope(factory) as session:
        candidate = session.get(rel.ReleaseCandidate, candidate_id)
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert candidate is not None and candidate.status == "validated"
        assert operation is not None and operation.import_run_id is not None


def test_native_postgresql_candidate_validation_gate_first_blocks_backfill_after_commit(
    core_db, tmp_path: Path
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from dish_pg.release import ReleaseCandidateService

    factory, context, task_id, target = _seed_target(core_db)
    candidate_id, bundle_id, validated_at = _seed_candidate_bundle(
        core_db, factory, context, task_id
    )
    snapshot, operation_id = _terminal_snapshot(
        tmp_path, task_id=task_id, suffix="validation-first"
    )

    validation_session = factory()
    backfill_session = factory()
    try:
        ReleaseCandidateService(validation_session).validate_candidate(
            candidate_id=candidate_id,
            evidence_bundle_id=bundle_id,
            validated_at=validated_at,
        )

        backfill_session.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            apply_terminal_history_snapshot(
                backfill_session,
                target=target,
                snapshot=snapshot,
                source_commit=SOURCE_COMMIT,
            )
        backfill_session.rollback()

        validation_session.commit()
    finally:
        backfill_session.close()
        validation_session.close()

    with pytest.raises(
        TerminalHistoryBackfillError,
        match="blocked after release-candidate validation",
    ):
        with session_scope(factory) as session:
            apply_terminal_history_snapshot(
                session,
                target=target,
                snapshot=snapshot,
                source_commit=SOURCE_COMMIT,
            )

    with session_scope(factory) as session:
        assert session.get(wf.WorkflowOperation, operation_id) is None
        assert int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0) == 1
