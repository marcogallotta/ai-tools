from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
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
    resolve_backfill_target,
)
from dish_pg.legacy_source import export_legacy_source
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedServiceLeaseSpec,
    ImportedTaskSpec,
    ImportedVerificationCycleSpec,
    ImportedWorkflowOperationSpec,
)
from tests.support.postgresql.core import HASH_A, NOW, _bootstrap_registry, _next, core_db

SOURCE_COMMIT = "9" * 40
TASK_GID = "1217304073198491"


def _row_state(row) -> tuple[tuple[str, object], ...]:
    return tuple((column.key, getattr(row, column.key)) for column in row.__table__.columns)


def _legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE task_content_state(
            task_gid TEXT PRIMARY KEY,last_confirmed_identity TEXT,last_confirmed_title TEXT,
            last_confirmed_notes TEXT,schema_version TEXT,confirmed_at TEXT);
        CREATE TABLE operations(
            operation_id TEXT PRIMARY KEY,task_gid TEXT,operation_kind TEXT,status TEXT,
            created_at TEXT,completed_at TEXT,phase TEXT,terminal_outcome TEXT);
        CREATE TABLE verification_cycles(
            cycle_id TEXT PRIMARY KEY,operation_id TEXT,task_gid TEXT,cycle_number INTEGER,
            outcome TEXT,created_at TEXT,completed_at TEXT);
        CREATE TABLE service_leases(
            lease_id TEXT PRIMARY KEY,operation_id TEXT,task_gid TEXT,owner_id TEXT,run_id TEXT,
            acquired_at TEXT,expires_at TEXT,released_at TEXT,lease_kind TEXT,
            actor_attempt_seq INTEGER,context_cycle_id TEXT);
        """
    )
    conn.execute(
        "INSERT INTO task_content_state VALUES (?,?,?,?,?,?)",
        (TASK_GID, "id-1", "Title", "Body", "schema-1", NOW.isoformat()),
    )
    conn.commit()
    conn.close()


def _insert_terminal_history(
    path: Path,
    *,
    operation_id: uuid.UUID,
    cycle_id: uuid.UUID,
    lease_id: uuid.UUID,
    kind: str = "planning",
    minute: int = 0,
) -> None:
    created = NOW + timedelta(minutes=minute)
    completed = created + timedelta(seconds=30)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (
            str(operation_id),
            TASK_GID,
            kind,
            "completed",
            created.isoformat(),
            completed.isoformat(),
            "terminal",
            "planning_handoff_confirmed",
        ),
    )
    conn.execute(
        "INSERT INTO verification_cycles VALUES (?,?,?,?,?,?,?)",
        (
            str(cycle_id),
            str(operation_id),
            TASK_GID,
            1,
            "approved",
            created.isoformat(),
            completed.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(lease_id),
            str(operation_id),
            TASK_GID,
            "owner-1",
            f"legacy-run-{minute}",
            created.isoformat(),
            (created + timedelta(minutes=5)).isoformat(),
            completed.isoformat(),
            "actor",
            minute + 1,
            str(cycle_id),
        ),
    )
    conn.commit()
    conn.close()


def _history_spec(
    *, operation_id: uuid.UUID, cycle_id: uuid.UUID, lease_id: uuid.UUID, kind: str = "planning"
) -> ImportedOperationHistorySpec:
    completed = NOW + timedelta(seconds=30)
    return ImportedOperationHistorySpec(
        operations=(
            ImportedWorkflowOperationSpec(
                operation_id=operation_id,
                kind=kind,
                status="completed",
                phase="terminal",
                terminal_outcome="planning_handoff_confirmed",
                created_at=NOW,
                completed_at=completed,
            ),
        ),
        verification_cycles=(
            ImportedVerificationCycleSpec(
                cycle_id=cycle_id,
                operation_id=operation_id,
                cycle_sequence=1,
                outcome="approved",
                created_at=NOW,
                completed_at=completed,
            ),
        ),
        leases=(
            ImportedServiceLeaseSpec(
                lease_id=lease_id,
                operation_id=operation_id,
                source_run_id="legacy-run-0",
                owner_id="owner-1",
                lease_kind="actor",
                actor_attempt_sequence=1,
                verification_cycle_id=cycle_id,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                released_at=completed,
            ),
        ),
    )


def _import_task(
    session,
    ids,
    context,
    *,
    task_id: uuid.UUID,
    history: ImportedOperationHistorySpec | None = None,
) -> None:
    CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        spec=ImportedTaskSpec(
            task_id=task_id,
            asana_task_gid=TASK_GID,
            title="[ready] Imported",
            body="Canonical body\n---\nStatus: ready\n",
            identity_scheme="legacy-sha256-v1",
            content_identity=HASH_A,
            project_ids=(context["project_id"],),
            section_id=context["section_id"],
            completed=False,
            observed_at=NOW,
            operation_history=history or ImportedOperationHistorySpec(),
        ),
    )


def _seed_target(core_db, *, history: ImportedOperationHistorySpec | None = None):
    factory, ids = core_db
    task_id = _next(ids)
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        _import_task(session, ids, context, task_id=task_id, history=history)
    with session_scope(factory) as session:
        target = resolve_backfill_target(session, task_gid=TASK_GID)
    return factory, context, task_id, target


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
    assert json.loads(snapshot.path.read_text())["format"] == "dish-terminal-history-backfill-source-v1"
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
        assert supplemental.provenance["source_format"] == "dish-terminal-history-backfill-source-v1"
        assert supplemental.provenance["source_record_count"] == 1
        assert supplemental.provenance["candidate_attestation"] == (
            "not-covered-by-current-release-candidate-contract"
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
