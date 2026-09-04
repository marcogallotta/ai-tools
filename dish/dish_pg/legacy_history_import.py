"""One-shot carry-forward of preserved SQLite workflow/admin facts into audit authority."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from sqlalchemy import select
from sqlalchemy.orm import Session
from dish_tool.admin import _durable_attention_items
from . import models
from . import stage3_models as wf
from .repositories import RegistryRepository
_NAMESPACE = uuid.UUID("53ca3493-f296-460f-947c-5394a391f6cb")
HISTORY_EVENT = "legacy_history_imported"
ATTENTION_EVENT = "legacy_attention_imported"
RESOLUTION_EVENT = "legacy_attention_resolved"
SNAPSHOT_EVENT = "legacy_history_snapshot_imported"
RECEIPT_EVENT = "legacy_history_import_receipt"
_EXPECTED_PENDING = frozenset((("1217832148041218", "e39a9346-b1bc-43bd-9c5b-abdb2172a1ff"), ("1217089887920602", "68ea7d3c-d540-46d7-8ba8-af0cee9e2ac4"), ("1217166788025562", "a153e5b2-7165-4063-baa8-f11d87660e45")))
class LegacyHistoryImportError(ValueError):
    pass
def _sha(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()
def _id(generation_id: uuid.UUID, snapshot_sha: str, kind: str, source_id: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{generation_id}:{snapshot_sha}:{kind}:{source_id}")
def capture_legacy_source(path: Path) -> tuple[dict[str, Any], str]:
    database = path.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        attention = [dict(item) for item in _durable_attention_items(conn)]
        for item in attention:
            operation = conn.execute("SELECT created_at,status,phase,terminal_outcome FROM operations WHERE operation_id=?", (item["operation_id"],)).fetchone()
            if operation is None: raise LegacyHistoryImportError(f"legacy queue operation disappeared: {item['operation_id']}")
            item["source_operation"] = dict(operation)
        rows = conn.execute(
            """SELECT audit.*,COALESCE(audit.task_gid,operation.task_gid) AS resolved_task_gid
                 FROM audit_events AS audit LEFT JOIN operations AS operation
                   ON operation.operation_id=audit.operation_id
                WHERE audit.result_ok=1 AND (audit.governed_kind='decision'
                   OR audit.event_type LIKE 'verification.%' OR audit.event_type LIKE 'human_review.%'
                   OR audit.event_type LIKE 'hold.%' OR audit.event_type LIKE 'semantic_proposal.%'
                   OR audit.event_type LIKE 'research.preconstruction_%' OR audit.event_type='marco.authorization')
                ORDER BY audit.created_at,audit.event_id"""
        ).fetchall()
        history: list[dict[str, Any]] = []
        operation_ids: set[str] = set()
        for row in rows:
            payload = dict(row)
            payload["details"] = json.loads(payload["details"])
            for key in ("before_state", "after_state", "actor_provenance"):
                if payload.get(key):
                    payload[key] = json.loads(payload[key])
            task_gid = str(payload.pop("resolved_task_gid") or "").strip() or None
            operation_id = str(payload.get("operation_id") or "").strip()
            if operation_id:
                operation_ids.add(operation_id)
            history.append({"kind": "audit_event", "source_id": str(row["event_id"]), "task_gid": task_gid, "occurred_at": str(row["created_at"]), "payload": payload})
        if operation_ids:
            marks = ",".join("?" for _ in operation_ids)
            for table, identity, occurred in (("operations", "operation_id", "completed_at"), ("verification_cycles", "cycle_id", "completed_at")):
                clause = f"operation_id IN ({marks}) AND completed_at IS NOT NULL"
                if table == "operations":
                    clause += " AND status IN ('completed','cancelled')"
                for row in conn.execute(f"SELECT * FROM {table} WHERE {clause} ORDER BY {occurred},{identity}", tuple(sorted(operation_ids))):
                    payload = dict(row)
                    history.append({"kind": f"terminal_{table[:-1]}", "source_id": str(payload[identity]), "task_gid": str(payload.get("task_gid") or "").strip() or None, "occurred_at": str(payload[occurred]), "payload": payload})
    finally:
        conn.close()
    source = {"format": "dish-legacy-workflow-history-v1", "attention": attention, "history": history}
    return source, _sha(source)
def _bindings(session: Session, generation_id: uuid.UUID, gids: set[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for gid in sorted(gids):
        stmt = select(models.TaskExternalAlias).where(models.TaskExternalAlias.external_system == "asana", models.TaskExternalAlias.external_id == gid)
        if session.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        aliases = list(session.scalars(stmt))
        if len(aliases) != 1 or aliases[0].state != "active":
            raise LegacyHistoryImportError(f"legacy task alias is missing, duplicate, or retired: {gid}")
        task_stmt = select(models.DishTask).where(models.DishTask.task_id == aliases[0].task_id)
        state_stmt = select(models.DishState).where(models.DishState.generation_id == generation_id, models.DishState.task_id == aliases[0].task_id)
        if session.get_bind().dialect.name == "postgresql":
            task_stmt, state_stmt = task_stmt.with_for_update(), state_stmt.with_for_update()
        task, state = session.scalar(task_stmt), session.scalar(state_stmt)
        if task is None or task.existence_state == "retired" or state is None:
            raise LegacyHistoryImportError(f"legacy task does not map to an active Dish in this generation: {gid}")
        result[gid] = {"task_id": str(task.task_id), "dish_version": state.dish_version, "content_version_id": str(state.current_content_version_id), "section_id": str(state.section_id) if state.section_id else None, "completed": state.completed, "archived_at": state.archived_at.isoformat() if state.archived_at else None}
    return result
def _existing_sources(session: Session, generation_id: uuid.UUID) -> dict[tuple[str, str], str]:
    events = session.scalars(select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.generation_id == generation_id, wf.GovernedAuditEvent.event_type.in_((HISTORY_EVENT, ATTENTION_EVENT))))
    result: dict[tuple[str, str], str] = {}
    for event in events:
        key = (str(event.payload.get("source_kind") or ""), str(event.payload.get("source_identity") or ""))
        digest = str(event.payload.get("source_record_sha256") or "")
        if key in result and result[key] != digest:
            raise LegacyHistoryImportError(f"existing legacy source identity has conflicting bytes: {key[1]}")
        result[key] = digest
    return result
def _store(session: Session, *, generation, run_id: uuid.UUID, binding, snapshot_sha: str, kind: str, source_id: str, record: Mapping[str, Any], task_id: uuid.UUID | None, now: datetime, event_type: str, existing_sources: dict[tuple[str, str], str]) -> None:
    record_sha = _sha(record)
    if event_type in {HISTORY_EVENT, ATTENTION_EVENT}:
        prior = existing_sources.get((kind, source_id))
        if prior is not None:
            if prior != record_sha:
                raise LegacyHistoryImportError(f"legacy source identity collides with different bytes: {source_id}")
            return
        existing_sources[(kind, source_id)] = record_sha
    request_id = _id(generation.generation_id, snapshot_sha, kind, source_id)
    payload = {"request_kind": "legacy_history_import", "source_snapshot_sha256": snapshot_sha, "source_kind": kind, "source_identity": source_id, "source_record_sha256": record_sha, "record": dict(record)}
    if session.get(wf.ServiceRequest, request_id) is not None:
        raise LegacyHistoryImportError(f"deterministic legacy request already exists without receipt: {source_id}")
    session.add(wf.ServiceRequest(request_id=request_id, generation_id=generation.generation_id, run_id=run_id, owner_id="legacy-history-import", principal_class="service", command_name="legacy-history-import", canonical_payload_sha256=_sha(payload), canonical_payload=payload, protocol_release=binding.protocol_release, dish_release=generation.dish_release, admitted_at=now))
    outcome = {"imported": True, "source_identity": source_id, "source_snapshot_sha256": snapshot_sha}
    session.add(wf.ServiceRequestOutcome(outcome_id=_id(generation.generation_id, snapshot_sha, f"{kind}-outcome", source_id), request_id=request_id, outcome_class="success", result_code="LEGACY_HISTORY_IMPORTED", http_status=200, result_payload=outcome, result_sha256=_sha(outcome), immutable_success=True, recorded_at=now))
    session.flush()
    session.add(wf.GovernedAuditEvent(audit_event_id=_id(generation.generation_id, snapshot_sha, f"{kind}-audit", source_id), generation_id=generation.generation_id, request_id=request_id, command_execution_id=None, task_id=task_id, operation_id=None, event_type=event_type, actor="legacy-history-import", payload=payload, occurred_at=now))
def apply_legacy_source(session: Session, *, source: Mapping[str, Any], snapshot_sha: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if source.get("format") != "dish-legacy-workflow-history-v1" or _sha(source) != snapshot_sha:
        raise LegacyHistoryImportError("legacy source payload/hash mismatch")
    generations = list(session.scalars(select(models.AuthorityGeneration).where(models.AuthorityGeneration.status == "active")))
    if len(generations) != 1:
        raise LegacyHistoryImportError("expected exactly one active PostgreSQL generation")
    generation = generations[0]
    attention, history = list(source["attention"]), list(source["history"])
    pending = [row for row in attention if bool(row.get("needs_you"))]
    pending_keys = {(str(row.get("task_gid") or ""), str(row.get("operation_id") or "")) for row in pending}
    if len(attention) != 27 or len(pending) != 3 or len(attention) - len(pending) != 24 or pending_keys != _EXPECTED_PENDING:
        raise LegacyHistoryImportError("legacy queue no longer matches the bounded 27/3/24 carry-forward source")
    gids = {str(row.get("task_gid") or "") for row in history + attention} - {""}
    before = _bindings(session, generation.generation_id, gids)
    receipt_request_id = _id(generation.generation_id, snapshot_sha, "receipt", "complete")
    existing_receipt = session.get(wf.ServiceRequest, receipt_request_id)
    if existing_receipt is not None:
        payload = existing_receipt.canonical_payload
        if payload.get("request_kind") != "legacy_history_import" or payload.get("source_snapshot_sha256") != snapshot_sha:
            raise LegacyHistoryImportError("deterministic receipt identity collides with different import")
        return dict(payload["record"]) | {"inserted": False}
    binding = RegistryRepository(session).active_release_contract(generation.generation_id).honest_binding
    run_id = _id(generation.generation_id, snapshot_sha, "run", "legacy-history-import")
    if session.get(wf.ServiceRun, run_id) is not None:
        raise LegacyHistoryImportError("legacy import run exists without a complete receipt")
    session.add(wf.ServiceRun(run_id=run_id, generation_id=generation.generation_id, owner_id="legacy-history-import", agent="service", capability_digest=hashlib.sha256(f"legacy-history:{generation.generation_id}:{snapshot_sha}".encode()).digest(), bootstrap_id=None, status="active", registered_at=now, retired_at=None))
    session.flush()
    existing_sources = _existing_sources(session, generation.generation_id)
    common = {"session": session, "generation": generation, "run_id": run_id, "binding": binding, "snapshot_sha": snapshot_sha, "now": now, "existing_sources": existing_sources}
    _store(**common, kind="snapshot", source_id="complete", record=source, task_id=None, event_type=SNAPSHOT_EVENT)
    for record in history:
        task_id = uuid.UUID(str(before[str(record["task_gid"])]["task_id"])) if record.get("task_gid") else None
        _store(**common, kind=str(record["kind"]), source_id=str(record["source_id"]), record=record, task_id=task_id, event_type=HISTORY_EVENT)
    pending_ids = []
    for record in pending:
        source_id = f"{record['task_gid']}:{record['operation_id']}"
        pending_ids.append(source_id)
        _store(**common, kind="attention", source_id=source_id, record=record, task_id=uuid.UUID(str(before[str(record["task_gid"])]["task_id"])), event_type=ATTENTION_EVENT)
    receipt = {"source_snapshot_sha256": snapshot_sha, "generation_id": str(generation.generation_id), "mapped_tasks": before, "attention_count": len(attention), "history_count": len(history), "pending_attention_count": len(pending), "pending_identities": sorted(pending_ids), "skipped_system_count": 24, "conflicts": []}
    _store(**common, kind="receipt", source_id="complete", record=receipt, task_id=None, event_type=RECEIPT_EVENT)
    if _bindings(session, generation.generation_id, gids) != before:
        raise LegacyHistoryImportError("mapped Dish identity changed before commit")
    run = session.get(wf.ServiceRun, run_id)
    run.status, run.retired_at = "retired", now
    session.flush()
    return receipt | {"inserted": True}
def unresolved_legacy_attention(session: Session, generation_id: uuid.UUID) -> list[dict[str, Any]]:
    imports = session.scalars(select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.generation_id == generation_id, wf.GovernedAuditEvent.event_type == ATTENTION_EVENT))
    resolved = {str(event.payload.get("attention_id") or "") for event in session.scalars(select(wf.GovernedAuditEvent).where(wf.GovernedAuditEvent.generation_id == generation_id, wf.GovernedAuditEvent.event_type == RESOLUTION_EVENT))}
    rows = []
    for event in imports:
        if str(event.audit_event_id) in resolved:
            continue
        item = dict(event.payload["record"])
        item.update({"attention_id": str(event.audit_event_id), "dish_id": str(event.task_id), "task_id": str(event.task_id), "source_operation_id": item.pop("operation_id", None)})
        signals = [dict(signal) for signal in item.get("signals", [])]
        if signals:
            signals[0]["shell_command"] = f"dish-admin resolve-legacy-attention {event.audit_event_id} --resolution TEXT"
        item["signals"] = signals
        rows.append(item)
    return rows
