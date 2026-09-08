from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import CommandCall
from dish_pg.database import session_scope
from dish_pg.native_catalog_runtime_finalizer import finalize_native_catalog_runtime_authority
from dish_pg.repositories import CatalogRepository
from tests.postgresql.test_native_section_catalog_foundation import (
    NOW,
    _stage_runtime_switch_fixture,
)
from tests.postgresql.test_native_section_lifecycle import _call, _view
from tests.support.postgresql.command import _port
from tests.support.postgresql.workflow import _next, _register_run

pytestmark = pytest.mark.database_boundary
pytest_plugins = ("tests.support.postgresql.core",)


def test_catalog_successor_rebinds_open_operation_head(core_db, monkeypatch) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        seeded, _expectation, _, _carry_event_id, occurrences = _stage_runtime_switch_fixture(
            session, ids, monkeypatch
        )
        finalize_native_catalog_runtime_authority(
            session, source_commit_sha="f" * 40, now=NOW + timedelta(hours=1)
        )
        run_id = _next(ids)
        _register_run(
            session,
            generation_id=seeded["generation_id"],
            run_id=run_id,
            owner="Marco",
        )
        target = next(
            occurrence
            for occurrence in occurrences
            if occurrence.verification_baseline_kind == "migration_assigned_ready"
        )
        contract = CatalogRepository(session).active_runtime_catalog_contract(
            seeded["generation_id"]
        )
        assert contract is not None
        port = _port(session, ids)
        started = port.execute(
            CommandCall(
                command_name="start",
                arguments={
                    "task_id": str(target.task_id),
                    "kind": "change",
                    "agent": "claude",
                },
                owner_id="Marco",
                principal_class="agent",
                run_id=run_id,
                request_id=_next(ids),
                now=NOW + timedelta(hours=2),
                protocol_release=contract.honest_binding.protocol_release,
            )
        )
        assert started.ok, (started.code, started.data)
        operation_id = uuid.UUID(started.data["operation_id"])
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert operation is not None and operation.lifecycle == "open"
        old_catalog_version_id = operation.catalog_version_id

        created = port.execute(
            _call(
                session,
                "create-section",
                run_id=run_id,
                request_id=_next(ids),
                generation_id=seeded["generation_id"],
                arguments={
                    **_view(session, seeded["generation_id"]),
                    "display_name": "Open-operation continuity",
                },
            )
        )
        assert created.ok, (created.code, created.data)
        new_catalog_version_id = uuid.UUID(created.data["catalog_version_id"])
        assert new_catalog_version_id != old_catalog_version_id
        assert created.data["rebound_open_operation_count"] >= 1

        session.refresh(operation)
        assert operation.catalog_version_id == new_catalog_version_id
        state = session.get(
            models.DishState, (seeded["generation_id"], target.task_id)
        )
        assert state is not None
        assert state.catalog_version_id == new_catalog_version_id

        audit = session.scalar(
            select(wf.GovernedAuditEvent)
            .where(
                wf.GovernedAuditEvent.event_type
                == "section_catalog_operation_rebinds"
            )
            .order_by(wf.GovernedAuditEvent.occurred_at.desc())
        )
        assert audit is not None
        assert str(operation_id) in audit.payload["operation_ids"]
        assert audit.payload["predecessor_catalog_version_id"] == str(
            old_catalog_version_id
        )
        assert audit.payload["catalog_version_id"] == str(new_catalog_version_id)
