"""Shared terminal-history backfill fixtures for SQLite and native PostgreSQL tests."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path

from dish_pg.database import session_scope
from dish_pg.history_backfill import resolve_backfill_target
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.services import (
    CoreAuthorityService,
    ImportedOperationHistorySpec,
    ImportedTaskSpec,
)
from tests.support.postgresql.core import HASH_A, NOW, _bootstrap_registry, _next

SOURCE_COMMIT = "9" * 40
TASK_GID = "1217304073198491"


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
        CREATE TABLE operation_run_revocations(
            revocation_id TEXT PRIMARY KEY,operation_id TEXT,owner_id TEXT,run_id TEXT,
            source_lease_id TEXT,reason TEXT,revoked_at TEXT);
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
    owner_id: str = "owner-1",
    source_run_id: str | None = None,
) -> None:
    created = NOW + timedelta(minutes=minute)
    completed = created + timedelta(seconds=30)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
        (
            str(operation_id), TASK_GID, kind, "completed", created.isoformat(),
            completed.isoformat(), "terminal", "planning_handoff_confirmed",
        ),
    )
    conn.execute(
        "INSERT INTO verification_cycles VALUES (?,?,?,?,?,?,?)",
        (
            str(cycle_id), str(operation_id), TASK_GID, 1, "approved",
            created.isoformat(), completed.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO service_leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            str(lease_id), str(operation_id), TASK_GID, owner_id,
            source_run_id or f"legacy-run-{minute}", created.isoformat(),
            (created + timedelta(minutes=5)).isoformat(), completed.isoformat(),
            "actor", minute + 1, str(cycle_id),
        ),
    )
    conn.commit()
    conn.close()


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
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head=ALEMBIC_HEAD
        )
        _import_task(session, ids, context, task_id=task_id, history=history)
    with session_scope(factory) as session:
        target = resolve_backfill_target(session, task_gid=TASK_GID)
    return factory, context, task_id, target
