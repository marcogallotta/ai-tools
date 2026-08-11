"""Runtime rehearsal: exercise the importer CLI against real PostgreSQL.

This does not repeat CoreAuthorityService's own correctness coverage. It
proves the *process*: dish_pg.importer.run_import driving the real service
across separately committed per-record transactions, using a real connection
pool, matching how the deployed importer will run.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.importer import run_import
from tests.support.postgresql.core import NOW, _bootstrap_registry, _import_one, _next, core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _record(context: dict[str, UUID], task_id: UUID, asana_gid: str) -> dict[str, object]:
    return {
        "task_id": str(task_id),
        "asana_task_gid": asana_gid,
        "title": "[ready] Exact imported task",
        "body": "Canonical body\n---\nStatus: ready\n",
        "identity_scheme": "legacy-sha256-v1",
        "content_identity": "a" * 64,
        "project_ids": [str(context["project_id"])],
        "section_id": str(context["section_id"]),
        "completed": False,
        "observed_at": NOW.isoformat(),
        "operation_history": {"operations": [], "leases": [], "verification_cycles": [], "revocations": []},
    }


def test_importer_persists_real_records_against_real_postgresql(core_db, tmp_path: Path) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        already_imported_task_id = _next(ids)
        _import_one(session, ids, context, task_id=already_imported_task_id, asana_gid="1")

    new_task_id = _next(ids)
    operation_id, lease_id = _next(ids), _next(ids)
    new_record = _record(context, new_task_id, "999999999")
    new_record["operation_history"] = {
        "operations": [{
            "operation_id": str(operation_id), "kind": "planning", "status": "completed",
            "phase": "terminal", "terminal_outcome": "planning_handoff_confirmed",
            "created_at": NOW.isoformat(), "completed_at": NOW.isoformat(),
        }],
        "leases": [{
            "lease_id": str(lease_id), "operation_id": str(operation_id),
            "source_run_id": "legacy-run-1", "owner_id": "legacy-owner",
            "lease_kind": "actor", "actor_attempt_sequence": 1,
            "verification_cycle_id": None, "issued_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
            "released_at": NOW.isoformat(),
        }],
        "verification_cycles": [],
        "revocations": [],
    }
    source = tmp_path / "tasks.ndjson"
    source.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                _record(context, already_imported_task_id, "1"),
                new_record,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    def prepare_import_run(session, generation_id, import_run_id, contract_binding_id) -> None:
        del session, generation_id, import_run_id, contract_binding_id

    def already_imported(session, task_id: UUID) -> bool:
        return session.get(models.DishTask, task_id) is not None

    summary = run_import(
        source=source,
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        session_factory=factory,
        prepare_import_run=prepare_import_run,
        already_imported=already_imported,
    )

    assert (summary.imported, summary.skipped, summary.failed, summary.exit_code) == (1, 1, 0, 0)

    with session_scope(factory) as session:
        imported = session.scalar(
            select(models.TaskExternalAlias).where(
                models.TaskExternalAlias.external_id == "999999999"
            )
        )
        assert imported is not None
        assert imported.task_id == new_task_id
        operation = session.get(wf.WorkflowOperation, operation_id)
        lease = session.get(wf.ServiceLease, lease_id)
        assert operation is not None and operation.import_run_id == context["import_run_id"]
        assert operation.creation_request_id is None and operation.creation_execution_id is None
        assert lease is not None and lease.run_id is None and lease.actor_attempt_sequence == 1
