from __future__ import annotations

import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import legacy_history_import as legacy
from dish_pg.database import session_scope
from dish_pg.frontend_admin_query import FrontendAdminQuery
from dish_service.frontend_admin import FrontendAdminConfig, FrontendAdminService
from dish_tool.database_initialization import initialize_database
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.core import _import_one
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

PENDING = (
    ("1217832148041218", "e39a9346-b1bc-43bd-9c5b-abdb2172a1ff", "human_review"),
    ("1217089887920602", "68ea7d3c-d540-46d7-8ba8-af0cee9e2ac4", "recovery"),
    ("1217166788025562", "a153e5b2-7165-4063-baa8-f11d87660e45", "recovery"),
)
PROTECTED = (
    models.DishTask,
    models.TaskExternalAlias,
    models.ContentVersion,
    models.DishMutationReceipt,
    models.DishState,
    models.TaskMembershipHead,
    wf.WorkflowOperation,
    wf.VerificationCycle,
    wf.ServiceLease,
    wf.EvidenceHold,
    wf.EvidenceHoldEvent,
    wf.HumanReviewRequirement,
    wf.HumanReviewDecision,
    wf.CookLogEntry,
)


def _attention(gid: str, operation_id: str, *, group: str, needs_you: bool) -> dict[str, object]:
    if group == "human_review":
        category, kind, summary = "needs_marco", "human_decision", "Make the Human Review decision."
    elif group == "recovery":
        category, kind, summary = "unsafe", "unresolved_execution", "An operation execution outcome is unresolved."
    else:
        category, kind, summary = "system", "inactive_run", "The operation has no active actor lease."
    return {
        "dish_id": gid,
        "task_gid": gid,
        "task_title": f"Legacy {gid}",
        "operation_ids": [operation_id],
        "operation_id": operation_id,
        "category": category,
        "needs_you": needs_you,
        "queue_group": group,
        "signals": [{"kind": kind, "category": category, "summary": summary}],
        "source_operation": {
            "created_at": NOW.isoformat(),
            "status": "open",
            "phase": "held_human" if group == "human_review" else "prepare_required",
            "terminal_outcome": None,
        },
    }


def _source() -> tuple[dict[str, object], str]:
    attention = [_attention(gid, op, group=group, needs_you=True) for gid, op, group in PENDING]
    for index in range(24):
        gid = f"98000000000{index:02d}"
        op = str(uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-system-{index}"))
        attention.append(_attention(gid, op, group="system", needs_you=False))
    first_gid, first_op, _ = PENDING[0]
    history = [{
        "kind": "audit_event",
        "source_id": "legacy-event-1",
        "task_gid": first_gid,
        "occurred_at": NOW.isoformat(),
        "payload": {
            "event_id": "legacy-event-1",
            "operation_id": first_op,
            "event_type": "human_review.dismissed",
            "details": {"decision": "legacy decision"},
        },
    }]
    source = {"format": "dish-legacy-workflow-history-v1", "attention": attention, "history": history}
    return source, legacy._sha(source)


def _seed(factory, ids, context) -> tuple[dict[str, object], str, dict[str, uuid.UUID]]:
    source, source_sha = _source()
    task_ids = {}
    with session_scope(factory) as session:
        for gid in sorted({str(row["task_gid"]) for row in source["attention"]}):
            task_ids[gid] = _import_one(session, ids, context, asana_gid=gid).task_id
    return source, source_sha, task_ids


def _protected_state(session) -> dict[str, tuple[tuple[object, ...], ...]]:
    result = {}
    for model in PROTECTED:
        columns = tuple(model.__table__.columns)
        order = tuple(model.__table__.primary_key.columns)
        result[model.__tablename__] = tuple(tuple(row) for row in session.execute(select(*columns).order_by(*order)).all())
    return result


def test_capture_legacy_source_is_read_only_and_uses_current_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = initialize_database(path)
    connection.close()
    before = path.read_bytes()
    source, source_sha = legacy.capture_legacy_source(path)
    assert source == {"format": "dish-legacy-workflow-history-v1", "attention": [], "history": []}
    assert source_sha == legacy._sha(source)
    assert path.read_bytes() == before


def test_import_is_atomic_idempotent_collision_safe_and_audit_only(workflow_db, monkeypatch) -> None:
    factory, ids, context, _ = workflow_db
    source, source_sha, _task_ids = _seed(factory, ids, context)
    with session_scope(factory) as session:
        before = _protected_state(session)

    original_store = legacy._store
    def fail_at_receipt(*args, **kwargs):
        original_store(*args, **kwargs)
        if kwargs["kind"] == "receipt":
            raise RuntimeError("forced import failure")
    monkeypatch.setattr(legacy, "_store", fail_at_receipt)
    with pytest.raises(RuntimeError, match="forced import failure"):
        with session_scope(factory) as session:
            legacy.apply_legacy_source(session, source=source, snapshot_sha=source_sha, now=NOW)
    monkeypatch.setattr(legacy, "_store", original_store)

    with session_scope(factory) as session:
        assert _protected_state(session) == before
        assert session.scalar(select(func.count()).select_from(wf.ServiceRun).where(wf.ServiceRun.owner_id == "legacy-history-import")) == 0
        receipt = legacy.apply_legacy_source(session, source=source, snapshot_sha=source_sha, now=NOW)
    assert receipt["inserted"] is True
    assert receipt["attention_count"] == 27
    assert receipt["pending_attention_count"] == 3
    assert receipt["skipped_system_count"] == 24
    assert set(receipt["pending_identities"]) == {f"{gid}:{op}" for gid, op, _ in PENDING}

    with session_scope(factory) as session:
        assert _protected_state(session) == before
        assert session.scalar(select(func.count()).select_from(wf.WorkflowOperation)) == 0
        assert session.scalar(select(func.count()).select_from(wf.HumanReviewDecision)) == 0
        assert session.scalar(select(func.count()).select_from(wf.EvidenceHold)) == 0
        history = list(session.scalars(select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.event_type == legacy.HISTORY_EVENT)))
        assert len(history) == 1
        assert history[0].operation_id is None
        assert history[0].payload["source_identity"] == "legacy-event-1"
        assert history[0].payload["record"]["occurred_at"] == NOW.isoformat()
        event_count = session.scalar(select(func.count()).select_from(wf.GovernedAuditEvent))
        replay = legacy.apply_legacy_source(session, source=source, snapshot_sha=source_sha, now=NOW)
        assert replay["inserted"] is False
        assert session.scalar(select(func.count()).select_from(wf.GovernedAuditEvent)) == event_count

    changed = copy.deepcopy(source)
    changed["history"][0]["payload"]["details"]["decision"] = "different bytes"
    changed_sha = legacy._sha(changed)
    with pytest.raises(legacy.LegacyHistoryImportError, match="collides with different bytes"):
        with session_scope(factory) as session:
            legacy.apply_legacy_source(session, source=changed, snapshot_sha=changed_sha, now=NOW)
    with session_scope(factory) as session:
        assert _protected_state(session) == before
        assert session.scalar(select(func.count()).select_from(wf.GovernedAuditEvent)) == event_count


def test_queue_frontend_and_resolution_use_only_imported_audit_attention(workflow_db) -> None:
    factory, ids, context, _ = workflow_db
    source, source_sha, task_ids = _seed(factory, ids, context)
    with session_scope(factory) as session:
        legacy.apply_legacy_source(session, source=source, snapshot_sha=source_sha, now=NOW)

    with session_scope(factory) as session:
        pending = legacy.unresolved_legacy_attention(session, context["generation_id"])
        assert {(row["task_gid"], row["source_operation_id"]) for row in pending} == {(gid, op) for gid, op, _ in PENDING}
        assert all("operation_id" not in row for row in pending)
        assert all("resolve-legacy-attention" in row["signals"][0]["shell_command"] for row in pending)

        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=admin_run, owner="marco", agent="marco")
        port = _port(session, ids)
        queued = port.execute(_call("queue", run_id=admin_run, principal="admin", owner="marco"))
        assert queued.ok
        imported = [row for row in queued.data["attention_items"] if row.get("attention_id")]
        assert len(imported) == 3
        assert {(row["task_gid"], row["source_operation_id"]) for row in imported} == {(gid, op) for gid, op, _ in PENDING}

        facts = FrontendAdminQuery(session).capture(projection_delay=timedelta(minutes=15))
        assert {(row["task_gid"], row["source_operation_id"]) for row in facts.legacy_attentions} == {(gid, op) for gid, op, _ in PENDING}
        payload = FrontendAdminService(
            FrontendAdminQuery(session),
            environment="test",
            config=FrontendAdminConfig(projection_delay=timedelta(minutes=15)),
        ).read()
        legacy_cards = {str(task_ids[gid]): group for gid, _op, group in PENDING}
        for card in payload["dishes"]:
            if card["task_id"] in legacy_cards:
                expected = "verification_attention" if legacy_cards[card["task_id"]] == "human_review" else "recovery_required"
                assert expected in card["diagnostics"]["attention_codes"]
        assert payload["summary"]["needs_you"] == 3

        target = imported[0]
        resolved = port.execute(_call(
            "resolve-legacy-attention",
            run_id=admin_run,
            request_id=_next(ids),
            principal="admin",
            owner="marco",
            arguments={"attention_id": target["attention_id"], "resolution": "Legacy case acknowledged; do not resurrect its workflow."},
        ))
        assert resolved.ok
        assert resolved.data["next_step"].startswith("Start a fresh native PostgreSQL operation")
        remaining = legacy.unresolved_legacy_attention(session, context["generation_id"])
        assert len(remaining) == 2
        assert target["attention_id"] not in {row["attention_id"] for row in remaining}
        imported_event = session.get(wf.GovernedAuditEvent, uuid.UUID(target["attention_id"]))
        assert imported_event is not None
        resolution = session.scalar(select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.event_type == legacy.RESOLUTION_EVENT))
        assert resolution is not None and resolution.operation_id is None
        assert resolution.payload["attention_id"] == target["attention_id"]
