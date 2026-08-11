from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.history_backfill import (
    TerminalHistoryBackfillError,
    apply_terminal_history_snapshot,
    capture_terminal_history_snapshot,
    resolve_backfill_target,
)
from dish_pg.legacy_source import export_legacy_source
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_RECONCILIATION_CONTRACT,
    EXACT_REVOCATION_SNAPSHOT_FORMAT,
    task_revocation_history_reconciled,
)
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedServiceLeaseSpec,
    ImportedTaskSpec,
    ImportedVerificationCycleSpec,
    ImportedWorkflowOperationSpec,
)
from dish_pg.workflow import (
    ExecutionSpec,
    ImportedRevocationHistoryUnreconciled,
    OperationRunRevoked,
    WorkflowAuthorityService,
)
from tests.support.postgresql.core import (
    HASH_A,
    NOW,
    _bootstrap_registry,
    _next,
    _uuid_stream,
    core_db,
)
from tests.support.postgresql.workflow import _admit, _register_run
from tests.support.postgresql.terminal_history_backfill import (
    SOURCE_COMMIT,
    TASK_GID,
    _import_task,
    _insert_terminal_history,
    _legacy_db,
    _seed_target,
)

ROOT = Path(__file__).resolve().parents[2]


def _row_state(row) -> tuple[tuple[str, object], ...]:
    return tuple((column.key, getattr(row, column.key)) for column in row.__table__.columns)






def _insert_exact_revocation(
    path: Path,
    *,
    revocation_id: uuid.UUID,
    operation_id: uuid.UUID,
    owner_id: str,
    source_run_id: str,
    source_lease_id: uuid.UUID | None,
    reason: str = "killed by Marco",
    minute: int = 1,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO operation_run_revocations VALUES (?,?,?,?,?,?,?)",
        (
            str(revocation_id),
            str(operation_id),
            owner_id,
            source_run_id,
            None if source_lease_id is None else str(source_lease_id),
            reason,
            (NOW + timedelta(minutes=minute)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _migration_config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _history_spec(
    *,
    operation_id: uuid.UUID,
    cycle_id: uuid.UUID,
    lease_id: uuid.UUID,
    kind: str = "planning",
    owner_id: str = "owner-1",
    source_run_id: str = "legacy-run-0",
    actor_attempt_sequence: int = 1,
    minute: int = 0,
) -> ImportedOperationHistorySpec:
    created = NOW + timedelta(minutes=minute)
    completed = created + timedelta(seconds=30)
    return ImportedOperationHistorySpec(
        operations=(
            ImportedWorkflowOperationSpec(
                operation_id=operation_id,
                kind=kind,
                status="completed",
                phase="terminal",
                terminal_outcome="planning_handoff_confirmed",
                created_at=created,
                completed_at=completed,
            ),
        ),
        verification_cycles=(
            ImportedVerificationCycleSpec(
                cycle_id=cycle_id,
                operation_id=operation_id,
                cycle_sequence=1,
                outcome="approved",
                created_at=created,
                completed_at=completed,
            ),
        ),
        leases=(
            ImportedServiceLeaseSpec(
                lease_id=lease_id,
                operation_id=operation_id,
                source_run_id=source_run_id,
                owner_id=owner_id,
                lease_kind="actor",
                actor_attempt_sequence=actor_attempt_sequence,
                verification_cycle_id=cycle_id,
                issued_at=created,
                expires_at=created + timedelta(minutes=5),
                released_at=completed,
            ),
        ),
    )






def test_open_operation_rejected_before_snapshot_or_import_mutation(core_db, tmp_path: Path) -> None:
    factory, _context, task_id, _target = _seed_target(core_db)
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    operation_id = uuid.uuid4()
    conn = sqlite3.connect(legacy)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (str(operation_id), TASK_GID, "planning", "running", NOW.isoformat(), None, "acting", None),
    )
    conn.commit(); conn.close()
    output = tmp_path / "snapshot.ndjson"
    with session_scope(factory) as session:
        before = int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0)
    with pytest.raises(TerminalHistoryBackfillError, match="status=running is non-terminal"):
        capture_terminal_history_snapshot(
            legacy_database=legacy, task_gid=TASK_GID, task_id=task_id, output=output
        )
    assert not output.exists()
    with session_scope(factory) as session:
        after = int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0)
    assert after == before


def test_open_verification_cycle_rejected(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    operation_id, cycle_id = uuid.uuid4(), uuid.uuid4()
    conn = sqlite3.connect(legacy)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (
            str(operation_id), TASK_GID, "planning", "completed", NOW.isoformat(),
            (NOW + timedelta(seconds=30)).isoformat(), "terminal", "planning_handoff_confirmed",
        ),
    )
    conn.execute(
        "INSERT INTO verification_cycles VALUES (?,?,?,?,?,?,?)",
        (str(cycle_id), str(operation_id), TASK_GID, 1, None, NOW.isoformat(), None),
    )
    conn.commit(); conn.close()
    output = tmp_path / "snapshot.ndjson"
    with pytest.raises(TerminalHistoryBackfillError, match="verification cycle is open"):
        capture_terminal_history_snapshot(
            legacy_database=legacy, task_gid=TASK_GID, task_id=uuid.uuid4(),
            output=output,
        )
    assert not output.exists()


def test_active_lease_rejected(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    operation_id, lease_id = uuid.uuid4(), uuid.uuid4()
    conn = sqlite3.connect(legacy)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (
            str(operation_id), TASK_GID, "planning", "completed", NOW.isoformat(),
            (NOW + timedelta(seconds=30)).isoformat(), "terminal", "planning_handoff_confirmed",
        ),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(lease_id), str(operation_id), TASK_GID, "owner-1", "legacy-run-1",
            NOW.isoformat(), (NOW + timedelta(minutes=5)).isoformat(), None, "actor", 1, None,
        ),
    )
    conn.commit(); conn.close()
    output = tmp_path / "snapshot.ndjson"
    with pytest.raises(TerminalHistoryBackfillError, match="service lease is active"):
        capture_terminal_history_snapshot(
            legacy_database=legacy, task_gid=TASK_GID, task_id=uuid.uuid4(),
            output=output,
        )
    assert not output.exists()


def test_terminal_backfill_is_provenance_safe_idempotent_and_preserves_task_authority(
    core_db, tmp_path: Path
) -> None:
    old_operation, old_cycle, old_lease = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    initial_history = _history_spec(
        operation_id=old_operation, cycle_id=old_cycle, lease_id=old_lease
    )
    factory, context, task_id, target = _seed_target(core_db, history=initial_history)
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy,
        operation_id=old_operation,
        cycle_id=old_cycle,
        lease_id=old_lease,
    )
    new_operation, new_cycle, new_lease = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _insert_terminal_history(
        legacy,
        operation_id=new_operation,
        cycle_id=new_cycle,
        lease_id=new_lease,
        minute=10,
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "terminal-history.ndjson",
    )
    assert json.loads(snapshot.path.read_text())["format"] == EXACT_REVOCATION_SNAPSHOT_FORMAT
    repeated_snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=snapshot.path,
    )
    assert repeated_snapshot.sha256 == snapshot.sha256

    with session_scope(factory) as session:
        task_before = session.get(models.DishTask, task_id)
        assert task_before is not None
        original_import_run = task_before.import_run_id
        primary_run_before = session.get(models.ImportRun, original_import_run)
        assert primary_run_before is not None
        original_alias = session.scalar(
            select(models.TaskExternalAlias).where(models.TaskExternalAlias.task_id == task_id)
        )
        assert original_alias is not None
        original_content = tuple(
            session.scalars(
                select(models.ContentVersion).where(models.ContentVersion.task_id == task_id)
            )
        )
        original_activation = tuple(
            session.scalars(
                select(models.ContentActivation).where(models.ContentActivation.task_id == task_id)
            )
        )
        original_memberships = tuple(
            session.scalars(
                select(models.TaskProjectMembershipEvent).where(
                    models.TaskProjectMembershipEvent.task_id == task_id
                )
            )
        )
        original_placements = tuple(
            session.scalars(
                select(models.TaskSectionPlacementEvent).where(
                    models.TaskSectionPlacementEvent.task_id == task_id
                )
            )
        )
        original_completions = tuple(
            session.scalars(
                select(models.TaskCompletionEvent).where(
                    models.TaskCompletionEvent.task_id == task_id
                )
            )
        )
        registry = session.get(models.SectionRegistryVersion, context["registry_version_id"])
        assert registry is not None
        preserved_before = {
            "primary_import_run": _row_state(primary_run_before),
            "task": _row_state(task_before),
            "alias": _row_state(original_alias),
            "content": tuple(_row_state(row) for row in original_content),
            "activation": tuple(_row_state(row) for row in original_activation),
            "memberships": tuple(_row_state(row) for row in original_memberships),
            "placements": tuple(_row_state(row) for row in original_placements),
            "completions": tuple(_row_state(row) for row in original_completions),
            "registry": _row_state(registry),
        }

        result = apply_terminal_history_snapshot(
            session,
            target=target,
            snapshot=snapshot,
            source_commit=SOURCE_COMMIT,
            clock=lambda: NOW + timedelta(hours=1),
        )
        supplemental_id = result.supplemental_import_run_id
        assert supplemental_id is not None
        assert (result.matched_operations, result.inserted_operations) == (1, 1)
        assert (result.matched_verification_cycles, result.inserted_verification_cycles) == (1, 1)
        assert (result.matched_leases, result.inserted_leases) == (1, 1)

    with session_scope(factory) as session:
        supplemental = session.get(models.ImportRun, supplemental_id)
        assert supplemental is not None
        assert supplemental.source_bundle_sha256 == snapshot.sha256
        assert supplemental.provenance["import_kind"] == "terminal-history-backfill-v1"
        assert supplemental.provenance["primary_import_run_id"] == str(original_import_run)
        assert supplemental.provenance["source_format"] == EXACT_REVOCATION_SNAPSHOT_FORMAT
        assert supplemental.provenance["source_record_count"] == 1
        assert supplemental.provenance["candidate_attestation"] == (
            "candidate-authority-v3+supplemental-terminal-history-v1"
        )
        assert session.get(wf.WorkflowOperation, old_operation).import_run_id == original_import_run
        assert session.get(wf.VerificationCycle, old_cycle).import_run_id == original_import_run
        assert session.get(wf.ServiceLease, old_lease).import_run_id == original_import_run
        assert session.get(wf.WorkflowOperation, new_operation).import_run_id == supplemental_id
        assert session.get(wf.VerificationCycle, new_cycle).import_run_id == supplemental_id
        assert session.get(wf.ServiceLease, new_lease).import_run_id == supplemental_id

        task_after = session.get(models.DishTask, task_id)
        alias_after = session.scalar(
            select(models.TaskExternalAlias).where(models.TaskExternalAlias.task_id == task_id)
        )
        registry_after = session.get(models.SectionRegistryVersion, context["registry_version_id"])
        assert task_after is not None and alias_after is not None and registry_after is not None
        preserved_after = {
            "primary_import_run": _row_state(session.get(models.ImportRun, original_import_run)),
            "task": _row_state(task_after),
            "alias": _row_state(alias_after),
            "content": tuple(
                _row_state(row)
                for row in session.scalars(
                    select(models.ContentVersion).where(models.ContentVersion.task_id == task_id)
                )
            ),
            "activation": tuple(
                _row_state(row)
                for row in session.scalars(
                    select(models.ContentActivation).where(
                        models.ContentActivation.task_id == task_id
                    )
                )
            ),
            "memberships": tuple(
                _row_state(row)
                for row in session.scalars(
                    select(models.TaskProjectMembershipEvent).where(
                        models.TaskProjectMembershipEvent.task_id == task_id
                    )
                )
            ),
            "placements": tuple(
                _row_state(row)
                for row in session.scalars(
                    select(models.TaskSectionPlacementEvent).where(
                        models.TaskSectionPlacementEvent.task_id == task_id
                    )
                )
            ),
            "completions": tuple(
                _row_state(row)
                for row in session.scalars(
                    select(models.TaskCompletionEvent).where(
                        models.TaskCompletionEvent.task_id == task_id
                    )
                )
            ),
            "registry": _row_state(registry_after),
        }
        assert preserved_after == preserved_before

    with session_scope(factory) as session:
        second = apply_terminal_history_snapshot(
            session,
            target=target,
            snapshot=snapshot,
            source_commit=SOURCE_COMMIT,
            clock=lambda: NOW + timedelta(hours=2),
        )
        assert second.supplemental_import_run_id == supplemental_id
        assert second.inserted_operations == 0
        assert second.inserted_verification_cycles == 0
        assert second.inserted_leases == 0
        assert int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0) == 2


def test_conflicting_stable_identity_rolls_back_supplemental_run(core_db, tmp_path: Path) -> None:
    operation_id, cycle_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conflicting_pg_history = _history_spec(
        operation_id=operation_id,
        cycle_id=cycle_id,
        lease_id=lease_id,
        kind="initial",
    )
    factory, _context, task_id, target = _seed_target(core_db, history=conflicting_pg_history)
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy,
        operation_id=operation_id,
        cycle_id=cycle_id,
        lease_id=lease_id,
        kind="planning",
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "terminal-history.ndjson",
    )
    with pytest.raises(TerminalHistoryBackfillError, match="stable identity conflicts"):
        with session_scope(factory) as session:
            apply_terminal_history_snapshot(
                session, target=target, snapshot=snapshot, source_commit=SOURCE_COMMIT
            )
    with session_scope(factory) as session:
        assert int(session.scalar(select(func.count()).select_from(models.ImportRun)) or 0) == 1


def test_pre0036_reconciliation_rejects_snapshot_missing_an_imported_operation(
    core_db, tmp_path: Path
) -> None:
    factory, ids = core_db
    task_id = _next(ids)
    first_operation = uuid.uuid4()
    second_operation = uuid.uuid4()
    first_cycle = uuid.uuid4()
    second_cycle = uuid.uuid4()
    first_lease = uuid.uuid4()
    second_lease = uuid.uuid4()
    first = _history_spec(
        operation_id=first_operation, cycle_id=first_cycle, lease_id=first_lease
    )
    second = _history_spec(
        operation_id=second_operation,
        cycle_id=second_cycle,
        lease_id=second_lease,
        actor_attempt_sequence=11,
        minute=10,
    )
    history = ImportedOperationHistorySpec(
        operations=first.operations + second.operations,
        verification_cycles=first.verification_cycles + second.verification_cycles,
        leases=first.leases + second.leases,
    )
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", exact_revocation_source=False
        )
        _import_task(session, ids, context, task_id=task_id, history=history)
    legacy = tmp_path / "legacy-partial-reconciliation.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy, operation_id=first_operation, cycle_id=first_cycle, lease_id=first_lease
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "partial-reconciliation.ndjson",
    )
    with session_scope(factory) as session:
        target = resolve_backfill_target(session, task_gid=TASK_GID)
        with pytest.raises(
            TerminalHistoryBackfillError, match="does not cover existing imported operations"
        ):
            apply_terminal_history_snapshot(
                session,
                target=target,
                snapshot=snapshot,
                source_commit=SOURCE_COMMIT,
                clock=lambda: NOW + timedelta(hours=1),
            )


def test_pre0036_import_can_attest_an_explicit_empty_revocation_set(
    core_db, tmp_path: Path
) -> None:
    factory, ids = core_db
    task_id = _next(ids)
    operation_id = uuid.uuid4()
    cycle_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    history = _history_spec(
        operation_id=operation_id, cycle_id=cycle_id, lease_id=lease_id
    )
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", exact_revocation_source=False
        )
        _import_task(session, ids, context, task_id=task_id, history=history)
    legacy = tmp_path / "legacy-no-revocations.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy, operation_id=operation_id, cycle_id=cycle_id, lease_id=lease_id
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "empty-revocation-reconciliation.ndjson",
    )
    with session_scope(factory) as session:
        target = resolve_backfill_target(session, task_gid=TASK_GID)
        result = apply_terminal_history_snapshot(
            session,
            target=target,
            snapshot=snapshot,
            source_commit=SOURCE_COMMIT,
            clock=lambda: NOW + timedelta(hours=1),
        )
        assert result.inserted_operations == 0
        assert result.inserted_verification_cycles == 0
        assert result.inserted_leases == 0
        assert result.inserted_revocations == 0
        assert result.supplemental_import_run_id is not None
        assert session.scalar(select(func.count()).select_from(wf.OperationRunRevocation)) == 0
        assert task_revocation_history_reconciled(
            session,
            generation_id=context["generation_id"],
            task_id=task_id,
            primary_import_run_id=context["import_run_id"],
        )


@pytest.mark.database_boundary
def test_0036_preexisting_import_fails_closed_until_exact_revocations_reconciled(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pre-0036-import.sqlite3"
    config = _migration_config(database)
    command.upgrade(config, "0035_persistence_constraint_integrity")

    run_id = uuid.uuid4()
    owner_id = "owner-1"
    operation_id = uuid.uuid4()
    other_operation_id = uuid.uuid4()
    cycle_id = uuid.uuid4()
    other_cycle_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    other_lease_id = uuid.uuid4()
    revocation_id = uuid.uuid4()

    first = _history_spec(
        operation_id=operation_id,
        cycle_id=cycle_id,
        lease_id=lease_id,
        owner_id=owner_id,
        source_run_id=str(run_id),
    )
    second = _history_spec(
        operation_id=other_operation_id,
        cycle_id=other_cycle_id,
        lease_id=other_lease_id,
        owner_id=owner_id,
        source_run_id=str(run_id),
        actor_attempt_sequence=11,
        minute=10,
    )
    imported_history = ImportedOperationHistorySpec(
        operations=first.operations + second.operations,
        verification_cycles=first.verification_cycles + second.verification_cycles,
        leases=first.leases + second.leases,
    )

    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    factory = sessionmaker(
        bind=engine, class_=Session, autoflush=False, expire_on_commit=False, future=True
    )
    ids = _uuid_stream()
    task_id = _next(ids)
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0035_persistence_constraint_integrity",
            exact_revocation_source=False,
        )
        _import_task(
            session, ids, context, task_id=task_id, history=imported_history
        )
    engine.dispose()

    command.upgrade(config, "head")
    assert ALEMBIC_HEAD == "0037_release_identity_contract"

    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    factory = sessionmaker(
        bind=engine, class_=Session, autoflush=False, expire_on_commit=False, future=True
    )
    pre_reconcile_execution = uuid.uuid4()
    with session_scope(factory) as session:
        target = resolve_backfill_target(session, task_gid=TASK_GID)
        _register_run(
            session, generation_id=context["generation_id"], run_id=run_id, owner=owner_id
        )
        workflow = WorkflowAuthorityService(session)
        request_id = uuid.uuid4()
        _admit(
            workflow,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner=owner_id,
            command="prepare",
            payload={"task_id": str(task_id)},
        )
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=pre_reconcile_execution,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=operation_id,
                command_name="prepare",
                transaction_profile="L",
                canonical_intent={"command": "prepare"},
                pinned_inputs={"now": NOW.isoformat()},
                contract_binding_id=context["binding_id"],
                admitted_at=NOW,
            )
        )
        with pytest.raises(
            ImportedRevocationHistoryUnreconciled, match="revocation history is unreconciled"
        ):
            workflow.create_actor_fact(
                actor_fact_id=uuid.uuid4(),
                execution_id=pre_reconcile_execution,
                operation_id=operation_id,
                run_id=run_id,
                owner_id=owner_id,
                actor_role="actor",
                agent="claude",
                actor_attempt_sequence=1,
                recorded_at=NOW,
            )

    legacy = tmp_path / "legacy-pre-0036.sqlite3"
    _legacy_db(legacy)
    _insert_terminal_history(
        legacy,
        operation_id=operation_id,
        cycle_id=cycle_id,
        lease_id=lease_id,
        owner_id=owner_id,
        source_run_id=str(run_id),
    )
    _insert_terminal_history(
        legacy,
        operation_id=other_operation_id,
        cycle_id=other_cycle_id,
        lease_id=other_lease_id,
        owner_id=owner_id,
        source_run_id=str(run_id),
        minute=10,
    )
    _insert_exact_revocation(
        legacy,
        revocation_id=revocation_id,
        operation_id=operation_id,
        owner_id=owner_id,
        source_run_id=str(run_id),
        source_lease_id=lease_id,
    )
    snapshot = capture_terminal_history_snapshot(
        legacy_database=legacy,
        task_gid=TASK_GID,
        task_id=task_id,
        output=tmp_path / "pre-0036-reconciliation.ndjson",
    )
    assert json.loads(snapshot.path.read_text())["format"] == EXACT_REVOCATION_SNAPSHOT_FORMAT

    with session_scope(factory) as session:
        result = apply_terminal_history_snapshot(
            session,
            target=target,
            snapshot=snapshot,
            source_commit=SOURCE_COMMIT,
            clock=lambda: NOW + timedelta(hours=1),
        )
        assert result.inserted_operations == 0
        assert result.inserted_verification_cycles == 0
        assert result.inserted_leases == 0
        assert result.inserted_revocations == 1
        assert result.supplemental_import_run_id is not None

    with session_scope(factory) as session:
        supplemental = session.get(models.ImportRun, result.supplemental_import_run_id)
        assert supplemental is not None
        assert supplemental.provenance[EXACT_REVOCATION_HISTORY_PROVENANCE_KEY] == (
            EXACT_REVOCATION_RECONCILIATION_CONTRACT
        )
        assert supplemental.provenance["source_format"] == EXACT_REVOCATION_SNAPSHOT_FORMAT
        revocations = tuple(
            session.scalars(
                select(wf.OperationRunRevocation).where(
                    wf.OperationRunRevocation.generation_id == context["generation_id"]
                )
            )
        )
        assert [(row.operation_id, row.owner_id, row.source_run_id) for row in revocations] == [
            (operation_id, owner_id, str(run_id))
        ]
        assert session.scalar(
            select(wf.OperationRunRevocation).where(
                wf.OperationRunRevocation.operation_id == other_operation_id
            )
        ) is None

        workflow = WorkflowAuthorityService(session)
        with pytest.raises(OperationRunRevoked):
            workflow.create_actor_fact(
                actor_fact_id=uuid.uuid4(),
                execution_id=pre_reconcile_execution,
                operation_id=operation_id,
                run_id=run_id,
                owner_id=owner_id,
                actor_role="actor",
                agent="claude",
                actor_attempt_sequence=1,
                recorded_at=NOW + timedelta(hours=1),
            )

        other_request_id = uuid.uuid4()
        other_execution_id = uuid.uuid4()
        _admit(
            workflow,
            request_id=other_request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner=owner_id,
            command="prepare",
            payload={"task_id": str(task_id), "operation_id": str(other_operation_id)},
        )
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=other_execution_id,
                request_id=other_request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=other_operation_id,
                command_name="prepare",
                transaction_profile="L",
                canonical_intent={"command": "prepare"},
                pinned_inputs={"now": NOW.isoformat()},
                contract_binding_id=context["binding_id"],
                admitted_at=NOW,
            )
        )
        actor = workflow.create_actor_fact(
            actor_fact_id=uuid.uuid4(),
            execution_id=other_execution_id,
            operation_id=other_operation_id,
            run_id=run_id,
            owner_id=owner_id,
            actor_role="actor",
            agent="claude",
            actor_attempt_sequence=2,
            recorded_at=NOW + timedelta(hours=1),
        )
        assert actor.operation_id == other_operation_id
        assert actor.run_id == run_id
    engine.dispose()


def test_allow_departed_tasks_export_behavior_is_unchanged(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.sqlite3"
    _legacy_db(legacy)
    manifest = tmp_path / "locations.json"
    manifest.write_text('{"tasks":{}}', encoding="utf-8")
    output = tmp_path / "legacy.ndjson"
    assert export_legacy_source(
        database=legacy,
        location_manifest=manifest,
        output=output,
        allow_departed_tasks=True,
    ) == 0
    assert output.read_text(encoding="utf-8") == ""
